"""Read-only account preflight of a PROD real-money release; it never sends an order.

`ops.py release-plan` judges the release (caps, window, DNA, LEAPS against FIX_C at a
price). This judges the account the release will trade, right before it is deployed, with
the same broker reads the tick makes (docs/AUDIT_20261103_TH.md, gate G3):

* buying power: USD buying power >= FIX_C + 1 % (BUYs are never refused for cash);
* open orders of the symbol: none (dispatch would be SUPPRESSED_ACTIVE_ORDER);
* the instrument profile: OC / US_STOCK / USD and its `fractionable` flag;
* every position row of the symbol has instrument_type EQUITY or OPTION (an untyped row
  is counted as shares by webull_io._extract_qty: LEAPS would be counted twice);
* the LEAPS actually held: long calls of the symbol x option_contract_multiplier must cover
  LEGO_LEAPS and expire after the approval window;
* the live price against the LEAPS band (release_horizon.leaps_price_band);
* the first order of a prefunded canary fits the canary caps; when it does not, the real
  shares to hold first;
* the order-history route the reconcile path depends on (a probe that matches no order).

No Firebase, no Preview, no Place, no Cancel: it imports none of them. Account id, token
and app credentials come from the environment and never reach the output.

    WEBULL_ENV=PROD WEBULL_APP_KEY=... WEBULL_APP_SECRET=... WEBULL_ACCOUNT_ID=... \\
        WEBULL_TOKEN_SECRET=projects/P/secrets/N WEBULL_TOKEN_DIR=$(mktemp -d) \\
        python3 tools/prod_live_preflight.py --symbol QBTS --fix-c 1500 --diff 50 \\
            --leaps 100 --allow-fractional true --window-sessions 5

Exit 0 when nothing blocks, 1 otherwise; the JSON report is the only thing on stdout.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_horizon as rh  # noqa: E402
from lego_one_row import Config  # noqa: E402
from lego_orders import PROD  # noqa: E402
from webull_io import (PROD_ENDPOINT, WebullConfigError, _extract_qty,  # noqa: E402
                       _retry_transient, account_id_is_listed, build_clients,
                       clients_endpoint, environment_label, error_reason, fetch_buying_power,
                       fetch_instrument_capability, fetch_open_orders, fetch_snapshot,
                       find_recent_order_by_client_id, redact_sensitive_text)

SCHEMA = "lego_prod_live_preflight_v1"
BLOCK, WARN, INFO = rh.BLOCK, rh.WARN, rh.INFO
# FIX_C + 1 %: the real-share exposure of the strategy is at most FIX_C (LEAPS worth nothing),
# bought at most 1 % above the decision price (MAX_DISPATCH_DRIFT), fees inside the margin.
CASH_FACTOR = Decimal("1") + rh.MAX_DISPATCH_DRIFT
DRIFT_FACTOR = Decimal("1") + rh.MAX_DISPATCH_DRIFT
LEAPS_EXPIRY_WARN_DAYS = 60
CENT = Decimal("0.01")
_POSITION_ERROR_KEYS = ("error", "error_code", "errorCode")


@dataclass(frozen=True)
class Params:
    symbol: str
    principal: Decimal
    diff: Decimal
    leaps: Decimal
    allow_fractional: bool
    window_end: datetime
    leaps_confirmed: bool = False
    interval_seconds: int = 900


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _position_rows(payload) -> list | None:
    """The rows of a positions answer, in the shapes webull_io._extract_qty accepts."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict) or any(payload.get(k) for k in _POSITION_ERROR_KEYS):
        return None
    for key in ("positions", "items", "data"):
        if key in payload:
            return payload[key] if isinstance(payload[key], list) else None
    return None


def _concerns(row, symbol: str) -> bool:
    if not isinstance(row, dict):
        return False
    if str(row.get("symbol") or "").strip().upper() == symbol:
        return True
    legs = row.get("legs")
    return isinstance(legs, list) and any(
        isinstance(leg, dict) and str(leg.get("symbol") or "").strip().upper() == symbol
        for leg in legs)


def leaps_held(rows: list, symbol: str) -> dict:
    """Long calls of *symbol* x multiplier, from the OPTION rows of a positions answer.

    The legs carry the option fields (developer.webull.co.th, Account Positions): option_type,
    quantity, option_contract_multiplier, option_expire_date. A short call, a put or a leg of
    another underlying is not a LEAPS; a leg that cannot be read makes the total unverified.
    """
    held, expiries, unreadable, found = Decimal(0), [], 0, 0
    for row in rows:
        if not (isinstance(row, dict) and row.get("instrument_type") == "OPTION"
                and _concerns(row, symbol)):
            continue
        found += 1
        legs = row.get("legs")
        if not isinstance(legs, list) or not legs:
            unreadable += 1
            continue
        for leg in legs:
            leg_symbol = leg.get("symbol") if isinstance(leg, dict) else None
            if not isinstance(leg_symbol, str) or not leg_symbol.strip():
                unreadable += 1
                continue
            if leg_symbol.strip().upper() != symbol:
                continue
            quantity = _decimal(leg.get("quantity"))
            multiplier = _decimal(leg.get("option_contract_multiplier"))
            kind = str(leg.get("option_type") or "").strip().upper()
            try:
                expiry = date.fromisoformat(str(leg.get("option_expire_date")))
            except ValueError:
                expiry = None
            if (quantity is None or multiplier is None or not multiplier > 0 or expiry is None
                    or kind not in ("CALL", "PUT")):
                unreadable += 1
                continue
            if kind == "CALL" and quantity > 0:
                held += quantity * multiplier
                expiries.append(expiry)
    return {"held": held, "expiries": expiries, "unreadable": unreadable, "found": found}


def prefund_range(principal: Decimal, leaps: Decimal, price: Decimal,
                  notional_cap: Decimal, quantity_cap: Decimal) -> tuple[int, int]:
    """Whole real shares whose first order fits the caps: |(LEAPS + R) x P - FIX_C| small enough."""
    room = min(notional_cap / DRIFT_FACTOR, quantity_cap * price)
    low = (principal - room) / price - leaps
    high = (principal + room) / price - leaps
    return max(0, math.ceil(low)), math.floor(high)


def judge(facts: dict, params: Params) -> dict:
    """The verdict on the gathered facts. Pure: no I/O, never raises on odd facts."""
    findings: list[dict] = []
    derived: dict = {}

    def add(id_: str, severity: str, message: str) -> None:
        findings.append({"id": id_, "severity": severity, "message": message})

    symbol = params.symbol.strip().upper()
    principal, diff, leaps = params.principal, params.diff, params.leaps
    derived["leaps_declared"] = _plain(leaps)

    if facts.get("environment") != PROD:
        add("environment", BLOCK, f"environment is {facts.get('environment')!r}: this preflight "
            "judges the production account of a real-money release only")
        return _report(facts, findings, derived)
    add("environment", INFO, "Production")
    if facts.get("endpoint_ok") is not True:
        add("endpoint", BLOCK, f"the authenticated clients are not bound to {PROD_ENDPOINT}")
    if facts.get("account_listed") is not True:
        add("account", BLOCK, "WEBULL_ACCOUNT_ID is not in the authenticated account list")

    instrument = facts.get("instrument") or {}
    if not instrument.get("ok") or instrument.get("fractionable") not in (True, False):
        add("instrument", BLOCK, f"the instrument profile of {symbol} was refused "
            f"({instrument.get('reason') or 'fractionable unknown'}); every decision would fail closed")
    else:
        add("instrument", INFO, f"{symbol}: OC, US_STOCK, USD")
        if instrument["fractionable"]:
            add("fractional", INFO, f"{symbol} is fractionable: orders at 2 decimal places"
                if params.allow_fractional else
                f"{symbol} is fractionable but LEGO_ALLOW_FRACTIONAL=false: whole shares")
        else:
            add("fractional", WARN if params.allow_fractional else INFO,
                f"{symbol} is not fractionable at Webull: orders will be whole shares "
                f"(lot {instrument.get('lot_size')}) although LEGO_ALLOW_FRACTIONAL=true"
                if params.allow_fractional else f"{symbol} trades in whole shares")

    rows, real = _position_rows(facts.get("positions")), None
    if rows is None:
        add("positions_shape", BLOCK, "the positions answer is not a readable list")
    else:
        untyped = [row for row in rows if _concerns(row, symbol)
                   and row.get("instrument_type") not in ("EQUITY", "OPTION")]
        if untyped:
            add("positions_shape", BLOCK, f"{len(untyped)} position row(s) of {symbol} carry no "
                "known instrument_type: the runtime would count them as shares (a LEAPS twice)")
        else:
            try:
                real = Decimal(str(_extract_qty(facts.get("positions"), symbol)))
            except Exception as exc:  # noqa: BLE001 - the runtime's own refusal, reported
                add("positions_shape", BLOCK, f"the positions of {symbol} are refused: "
                    f"{redact_sensitive_text(exc)[:160]}")
    if real is not None:
        derived["real_shares"] = _plain(real)

    if leaps > 0 and rows is not None:
        found = leaps_held(rows, symbol)
        derived["leaps_held"] = _plain(found["held"])
        if found["unreadable"] or not found["found"]:
            why = (f"no option position of {symbol} was found" if not found["found"] else
                   f"{found['unreadable']} option leg(s) of {symbol} could not be read")
            if params.leaps_confirmed:
                add("leaps_held", WARN, f"LEGO_LEAPS {_plain(leaps)} not verified from positions "
                    f"({why}); the owner confirmed them with LEGO_LEAPS_CONFIRMED=yes")
            else:
                add("leaps_held", BLOCK, f"LEGO_LEAPS {_plain(leaps)} cannot be verified: {why}. "
                    "Check the calls in the Webull app; LEGO_LEAPS_CONFIRMED=yes only if they are held")
        elif found["held"] < leaps:
            add("leaps_held", BLOCK, f"the account holds {_plain(found['held'])} share-equivalents of "
                f"long {symbol} calls, fewer than LEGO_LEAPS {_plain(leaps)}: the rebalance would "
                "count LEAPS that do not exist")
        else:
            add("leaps_held", INFO, f"long {symbol} calls: {_plain(found['held'])} share-equivalents "
                f">= LEGO_LEAPS {_plain(leaps)}")
        window_day = params.window_end.astimezone(timezone.utc).date()
        if found["expiries"]:
            earliest = min(found["expiries"])
            derived["leaps_earliest_expiry"] = earliest.isoformat()
            if earliest <= window_day:
                add("leaps_expiry", BLOCK, f"a counted {symbol} call expires {earliest}, not after the "
                    f"window ends {window_day}: the LEAPS base would vanish mid-release")
            elif earliest <= window_day + timedelta(days=LEAPS_EXPIRY_WARN_DAYS):
                add("leaps_expiry", WARN, f"a counted {symbol} call expires {earliest}, within "
                    f"{LEAPS_EXPIRY_WARN_DAYS} days of the window end: plan the roll and LEGO_LEAPS")
            else:
                add("leaps_expiry", INFO, f"earliest counted call expires {earliest}")
        else:
            derived["leaps_earliest_expiry"] = None
            add("leaps_expiry", WARN, "the expiry of the LEAPS could not be read: check it in the app")

    quote = facts.get("quote") or {}
    price = _decimal(quote.get("price"))
    if price is None or not price > 0:
        add("quote", BLOCK, f"no positive last price of {symbol}: the release cannot be planned on it")
        price = None
    else:
        derived["reference_price"] = _plain(price)
        derived["quote_time"] = quote.get("quote_time")

    tradable = price is not None
    if price is not None and leaps > 0:
        buy_below, sell_below = rh.leaps_price_band(principal, diff, leaps)
        derived["price_band"] = {"buy_below": _plain(buy_below), "sell_below": _plain(sell_below)}
        if price >= sell_below:
            tradable = False
            add("leaps_value", BLOCK, f"at {_plain(price)} the LEAPS alone are worth "
                f"{_money(leaps * price)} USD, at least FIX_C {_plain(principal)}: no order can be sent "
                f"(BUY needs < {_plain(buy_below)}, SELL < {_plain(sell_below)})")
        elif price >= buy_below:
            add("leaps_value", WARN, f"at {_plain(price)} no BUY is possible (needs < "
                f"{_plain(buy_below)}); only real shares above the target are sold")
        else:
            add("leaps_value", INFO, f"at {_plain(price)} BUY and SELL are both possible "
                f"(BUY < {_plain(buy_below)}, SELL < {_plain(sell_below)})")

    if tradable and real is not None:
        effective = real + leaps
        gap = effective * price - principal
        target = principal / price - leaps
        caps = rh.recommend_limits(principal, price, profile="PROD",
                                   interval_seconds=params.interval_seconds)
        notional_cap = Decimal(caps["LEGO_MAX_ORDER_NOTIONAL_USD"])
        quantity_cap = Decimal(caps["LEGO_MAX_ORDER_QUANTITY"])
        low, high = prefund_range(principal, leaps, price, notional_cap, quantity_cap)
        suggested = max(0, math.floor(target))
        derived.update({
            "effective_shares": _plain(effective), "value_usd": _money(effective * price),
            "gap_usd": _money(gap),
            "real_target": _plain(max(target, Decimal(0)).quantize(CENT, rounding=ROUND_DOWN)),
            "caps": {key: caps[key] for key in ("LEGO_MAX_ORDER_QUANTITY",
                                                "LEGO_MAX_ORDER_NOTIONAL_USD",
                                                "LEGO_MAX_SESSION_ORDERS")},
            "prefund_range": [str(low), str(high)] if low <= high else None,
            "suggested_real_shares": str(suggested),
        })
        if abs(gap) <= diff:
            derived["first_order"] = None
            add("first_order", INFO, f"{_plain(real)} real + {_plain(leaps)} LEAPS at {_plain(price)} "
                f"is within DIFF of FIX_C: no order until the price moves")
        else:
            side = "SELL" if gap > 0 else "BUY"
            quantity = (abs(gap) / price).quantize(CENT, rounding=ROUND_DOWN)
            derived["first_order"] = {"side": side, "notional_usd": _money(abs(gap)),
                                      "quantity": _plain(quantity)}
            if abs(gap) * DRIFT_FACTOR <= notional_cap and abs(gap) / price <= quantity_cap:
                add("first_order", INFO, f"the first order, a {side} of about {_money(abs(gap))} USD, "
                    f"fits the canary caps ({_plain(notional_cap)} USD / {_plain(quantity_cap)} shares)")
            else:
                hold = (f"hold between {low} and {high} real {symbol} shares first (about "
                        f"{suggested}; quantity with at most 2 decimals)" if low <= high else
                        "no whole number of real shares fits these caps")
                add("first_order", BLOCK, f"the first order would be a {side} of about "
                    f"{_money(abs(gap))} USD ({_plain(quantity)} shares), more than the canary caps "
                    f"allow ({_plain(notional_cap)} USD / {_plain(quantity_cap)} shares): every slot "
                    f"would be EXECUTION_LIMIT_BLOCKED. This release is prefunded only: {hold}, "
                    "then run it again")
    elif real is not None and price is not None:
        derived["real_target"] = "0"

    buying_power = _decimal(facts.get("buying_power_usd"))
    required = (principal * CASH_FACTOR).quantize(CENT, rounding=ROUND_CEILING)
    derived["required_cash_usd"] = str(required)
    derived["buying_power_usd"] = None if buying_power is None else _money(buying_power)
    if buying_power is None:
        add("buying_power", BLOCK, "USD buying power could not be read")
    elif buying_power < required:
        add("buying_power", BLOCK, f"USD buying power {_money(buying_power)} is below the "
            f"{required} this release may need (FIX_C + 1 %): a BUY would be refused at Preview")
    else:
        add("buying_power", INFO, f"USD buying power {_money(buying_power)} >= {required}")

    orders = facts.get("open_orders")
    if not isinstance(orders, list):
        add("open_orders", BLOCK, f"the open orders of {symbol} could not be read")
    elif orders:
        add("open_orders", BLOCK, f"{len(orders)} open order(s) of {symbol}: every dispatch would be "
            "SUPPRESSED_ACTIVE_ORDER; cancel or let them finish first")
    else:
        add("open_orders", INFO, f"no open order of {symbol}")

    if (facts.get("history_probe") or {}).get("ok") is not True:
        add("order_history_route", BLOCK, "the order-history query the reconcile path relies on "
            "did not answer as expected")
    return _report(facts, findings, derived)


def _report(facts: dict, findings: list, derived: dict) -> dict:
    blocking = [f["id"] for f in findings if f["severity"] == BLOCK]
    return {"schema": SCHEMA, "ok": not blocking, "environment": facts.get("environment"),
            "broker_mutations": False, "blocking": blocking,
            "warnings": [f["id"] for f in findings if f["severity"] == WARN],
            "findings": findings, "derived": derived}


def gather(symbol: str, *, now: datetime | None = None) -> dict:
    """The facts, from the broker reads the tick itself makes. Reads only."""
    now = now or datetime.now(timezone.utc)
    symbol = symbol.strip().upper()
    facts: dict = {"environment": environment_label()}
    if facts["environment"] != PROD:
        return facts
    trade, data = build_clients()
    facts["endpoint_ok"] = clients_endpoint(trade, data) == PROD_ENDPOINT
    account_id = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
    response = trade.account_v2.get_account_list()
    facts["account_listed"] = bool(getattr(response, "status_code", None) == 200
                                   and account_id_is_listed(response.json(), account_id))
    try:
        capability = fetch_instrument_capability(trade, symbol)
        facts["instrument"] = {
            "ok": True, "fractionable": capability.fractionable,
            "lot_size": _plain(capability.lot_size),
            "quantity_increment": _plain(capability.quantity_increment),
            "decimal_precision": capability.decimal_precision}
    except WebullConfigError as exc:
        facts["instrument"] = {"ok": False, "reason": error_reason(exc) or type(exc).__name__}
    facts["buying_power_usd"] = str(fetch_buying_power(trade))
    facts["positions"] = _retry_transient(
        lambda: trade.account_v2.get_account_position(account_id).json())
    facts["open_orders"] = fetch_open_orders(trade, symbol)
    probe = ("legopreflight" + uuid.uuid4().hex)[:32]
    try:
        matched = find_recent_order_by_client_id(
            trade, probe, placed_at=(now - timedelta(days=1)).isoformat())
        facts["history_probe"] = {"ok": matched is None}
    except Exception as exc:  # noqa: BLE001 - recorded as a failed proof
        facts["history_probe"] = {"ok": False, "error": type(exc).__name__}
    snapshot = fetch_snapshot(trade, data, Config(symbol, 1.0))
    facts["quote"] = {"price": str(snapshot["price"]), "quote_time": snapshot.get("quote_time")}
    return facts


def run(params: Params) -> dict:
    """gather + judge; anything that goes wrong is a BLOCK, never a pass."""
    try:
        facts = gather(params.symbol)
    except Exception as exc:  # noqa: BLE001 - fail closed with the type only
        cause = exc.__context__
        reason = type(exc).__name__ + (f" (while handling {type(cause).__name__})" if cause else "")
        report = _report({}, [{"id": "preflight_unavailable", "severity": BLOCK, "message": (
            f"the account could not be read: {reason}: {redact_sensitive_text(exc)[:200]}")}], {})
        report["environment"] = None
        return report
    return judge(facts, params)


def _positive(text: str, name: str, *, zero: bool = False) -> Decimal:
    number = _decimal(text)
    if number is None or number < 0 or (number == 0 and not zero):
        raise ValueError(f"{name} must be a finite number {'>= 0' if zero else '> 0'}")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--fix-c", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--leaps", required=True)
    parser.add_argument("--allow-fractional", required=True)
    parser.add_argument("--window-sessions", type=int, default=5)
    parser.add_argument("--leaps-confirmed", action="store_true")
    args = parser.parse_args(argv)
    try:
        symbol = args.symbol.strip().upper()
        if not symbol or not all(c.isalnum() or c in ".-" for c in symbol):
            raise ValueError("--symbol must be a ticker")
        if args.allow_fractional not in ("true", "false"):
            raise ValueError("--allow-fractional must be true or false")
        now = datetime.now(timezone.utc)
        params = Params(
            symbol=symbol, principal=_positive(args.fix_c, "--fix-c"),
            diff=_positive(args.diff, "--diff", zero=True),
            leaps=_positive(args.leaps, "--leaps", zero=True),
            allow_fractional=args.allow_fractional == "true",
            window_end=rh.window_end_after_sessions(now, args.window_sessions),
            leaps_confirmed=args.leaps_confirmed)
    except ValueError as exc:
        report = _report({}, [{"id": "arguments", "severity": BLOCK, "message": str(exc)}], {})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    # Every broker call logs one lego_operation line on stdout (tick_runtime.phase); they go
    # to stderr here so stdout carries the report alone.
    with contextlib.redirect_stdout(sys.stderr):
        report = run(params)
    report["window_end_utc"] = rh.format_window_end(params.window_end)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
