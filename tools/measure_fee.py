"""Measure the broker fee per order with Preview only; places and cancels nothing.

`release-plan --fee-pct` wants the fee as a percent of notional, and PROD has never
been measured: the only figure on record is UAT's 1.074% per order. This asks the
broker for Preview Order estimates at a few notionals and reports estimated fee /
estimated cost. It is also the first proof that PROD accepts the payload the tick
sends (MARKET / DAY / QTY / CORE), which UAT cannot show.

It imports no Place or Cancel helper and no Firebase. AUTO_SUBMIT must be false and a
PROD run needs --allow-prod. The one assumption not checked here: that a Preview
creates no order. That comes from the Webull documentation index; its full page could
not be opened when this was written, so confirm it with Webull before the PROD run.

    AUTO_SUBMIT=false WEBULL_ENV=UAT LEGO_SYMBOL=UBER LEGO_FIX_C=10000 \\
        python tools/measure_fee.py --notionals 25,50,100,250
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lego_broker_smoke import SmokeRefusal, _contains_account_id  # noqa: E402
from lego_orders import PROD, UAT  # noqa: E402
from webull_io import (PROD_ENDPOINT, UAT_ENDPOINT, broker_error_details,  # noqa: E402
                       build_clients, build_order_payload, clients_endpoint,
                       environment_label, fetch_holdings, fetch_instrument_capability,
                       fetch_snapshot, load_config, preview_market_order_result,
                       redact_sensitive_text)

SCHEMA = "lego_measure_fee_v1"
DEFAULT_NOTIONALS = "25,50,100,250,1000"
MAX_NOTIONALS = 10
PERCENT_STEP = Decimal("0.001")


def parse_notionals(text: str) -> list[Decimal]:
    values = []
    for part in str(text).split(","):
        try:
            value = Decimal(part.strip())
        except (InvalidOperation, ValueError) as exc:
            raise SmokeRefusal("notionals must be comma-separated positive numbers") from exc
        if not value.is_finite() or value <= 0:
            raise SmokeRefusal("notionals must be comma-separated positive numbers")
        values.append(value)
    if not values or len(values) > MAX_NOTIONALS or len(set(values)) != len(values):
        raise SmokeRefusal(f"give 1-{MAX_NOTIONALS} distinct notionals")
    return values


def quantity_for(notional: Decimal, price: Decimal, increment: Decimal) -> Decimal:
    """Whole multiples of the instrument's increment, never above the notional."""
    if not increment.is_finite() or increment <= 0:
        raise SmokeRefusal("instrument quantity increment is not usable")
    return ((notional / price) / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def fee_percent(preview: dict) -> Decimal:
    """estimated_transaction_fee / estimated_cost x 100, from a strict Preview result."""
    cost = Decimal(preview["estimated_cost"])
    fee = Decimal(preview["estimated_transaction_fee"])
    if not cost.is_finite() or not fee.is_finite() or cost <= 0 or fee < 0:
        raise ValueError("preview cost and fee must be finite, cost > 0, fee >= 0")
    return fee / cost * 100


def suggested_fee_pct(samples: list[dict]) -> str | None:
    """The largest measured percent (each sample is already rounded up): a per-order
    minimum fee makes the smallest order the costliest, so a median or mean would
    understate it."""
    measured = [Decimal(s["fee_pct"]) for s in samples if s.get("fee_pct") is not None]
    if not measured:
        return None
    return str(max(measured).quantize(PERCENT_STEP, rounding=ROUND_CEILING))


def measure(*, notionals: list[Decimal], side: str = "BUY", allow_prod: bool = False) -> dict:
    if os.environ.get("AUTO_SUBMIT", "").strip().lower() != "false":
        raise SmokeRefusal("AUTO_SUBMIT must be explicitly false while measuring the fee")
    side = str(side).upper()
    if side not in {"BUY", "SELL"}:
        raise SmokeRefusal("side must be BUY or SELL")
    environment = environment_label()
    if environment == PROD and not allow_prod:
        raise SmokeRefusal(
            "PROD Preview needs --allow-prod; it reads a real account and has never been run here")
    if not os.environ.get("WEBULL_ACCOUNT_ID", "").strip():
        raise SmokeRefusal("WEBULL_ACCOUNT_ID is required")

    config = load_config()
    trade_client, data_client = build_clients()
    expected = UAT_ENDPOINT if environment == UAT else PROD_ENDPOINT
    if str(clients_endpoint(trade_client, data_client) or "") != expected:
        raise SmokeRefusal("authenticated client endpoint does not match the validated environment")
    accounts = trade_client.account_v2.get_account_list()
    if getattr(accounts, "status_code", None) != 200:
        raise SmokeRefusal("account list returned an unexpected HTTP status")
    if not _contains_account_id(accounts.json(), os.environ["WEBULL_ACCOUNT_ID"].strip()):
        raise SmokeRefusal("configured account is not present in authenticated account list")

    capability = fetch_instrument_capability(trade_client, config.symbol)
    snapshot = fetch_snapshot(trade_client, data_client, config)
    price = Decimal(str(snapshot["price"]))
    if not price.is_finite() or price <= 0:
        raise SmokeRefusal("market snapshot has no usable price")
    holdings = Decimal(str(fetch_holdings(trade_client, config))) if side == "SELL" else None
    preview_config = replace(config, decimal_precision=capability.decimal_precision,
                             quantity_increment=float(capability.quantity_increment))
    quote_time = datetime.fromisoformat(snapshot["quote_time"].replace("Z", "+00:00"))
    quote_age = max(0.0, (datetime.now(timezone.utc) - quote_time).total_seconds())

    samples, shape = [], {}
    for notional in notionals:
        sample = {"notional_target": str(notional)}
        samples.append(sample)
        quantity = quantity_for(notional, price, capability.quantity_increment)
        if quantity <= 0:
            sample["error"] = "notional is below one quantity increment at this price"
            continue
        if holdings is not None and quantity > holdings:
            sample["error"] = "SELL quantity exceeds broker holdings"
            continue
        sample["quantity"] = str(quantity)
        try:
            payload = build_order_payload(
                preview_config, side, float(quantity), ("legofee" + uuid.uuid4().hex)[:32])
            if Decimal(payload[0]["quantity"]) != quantity:
                raise SmokeRefusal("quantity needs more decimal places than the instrument allows")
            shape = {key: payload[0].get(key) for key in (
                "order_type", "time_in_force", "entrust_type", "support_trading_session")}
            preview = preview_market_order_result(trade_client, payload, strict=True)
            sample.update(estimated_cost=preview["estimated_cost"],
                          estimated_transaction_fee=preview["estimated_transaction_fee"],
                          fee_pct=str(fee_percent(preview).quantize(PERCENT_STEP, rounding=ROUND_CEILING)))
        except Exception as exc:  # one refused Preview must not hide the others
            sample["error"] = redact_sensitive_text(f"{type(exc).__name__}: {exc}")
            sample["broker_error"] = broker_error_details(exc)

    suggestion = suggested_fee_pct(samples)
    return {
        "schema": SCHEMA,
        "environment": environment,
        "symbol": config.symbol.upper(),
        "side": side,
        "ok": suggestion is not None,
        "price": str(price),
        "quote_time": snapshot["quote_time"],
        "quote_age_seconds": round(quote_age, 1),
        "payload_shape": shape,
        "samples": samples,
        "suggested_fee_pct": suggestion,
        "basis": "largest measured percent, rounded up",
        "use": ("python ops.py release-plan --fee-pct " + suggestion) if suggestion else None,
        "broker_mutations": False,
    }


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise SmokeRefusal("invalid command-line arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--notionals", default=DEFAULT_NOTIONALS,
                        help=f"comma-separated USD order sizes (default {DEFAULT_NOTIONALS})")
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    parser.add_argument("--allow-prod", action="store_true",
                        help="permit Preview against the PROD account")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        report = measure(notionals=parse_notionals(args.notionals), side=args.side,
                         allow_prod=args.allow_prod)
    except Exception as exc:
        report = {"schema": SCHEMA, "ok": False, "broker_mutations": False,
                  "error_type": type(exc).__name__,
                  "error": redact_sensitive_text(str(exc)),
                  "broker_error": broker_error_details(exc)}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
