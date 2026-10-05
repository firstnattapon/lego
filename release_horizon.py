"""Pure release-horizon assessment for one deployment. No I/O, no renewal.

A release carries horizons that end silently: the approval window
(LEGO_TRADING_WINDOW_END), the DNA gate array and, in production, the broker
token. The 2026-10-01..05 UAT evidence showed two of them: a 24-hour window
covered a single regular session, after which every tick reported
RELEASE_EXPIRED for four days while the DNA itself was due to end that week.
Nothing refused to deploy such a release, and its caps (36,000 USD against a
10,000 USD principal) could never bind.

Before anything is deployed this module answers whether a release can carry
continuous trading and whether its limits mean anything:

* ``assess``            findings (BLOCK / WARN / INFO) for one release
* ``recommend_limits``  principal-proportional caps, including the t0 funding
                        release (the first order of a flat account is ~FIX_C)
* ``prod_live_ack``     the acknowledgement a production deployment must echo
                        before the runtime will send a real-money order

It never extends a window, a DNA array or a token. Renewal stays a reviewed
operator action; this only makes an unfit release visible while it can still be
fixed. A BLOCK is enforced only for a deployment that can trade (mode=trade and
active=true); an observe/inactive deployment gets the same findings as WARN.

Static findings depend only on the release itself, so the production
acknowledgement derived from them does not flip as time passes. Time-dependent
findings (window expired, too few sessions left) are produced only when ``now``
is supplied.
"""
from __future__ import annotations

import hmac
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

from dna_engine import decode_dna
from execution_limits import ExecutionLimitError, ExecutionLimits
from market_clock import CALENDAR_RULES_VERSION
from operational_health import complete_sessions_between, dna_end, regular_sessions

UTC = timezone.utc
BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"

# 09:30-16:00 New York. Orders are one-per-slot, so slots in a full session is
# the structural maximum of orders per market day.
FULL_SESSION_SECONDS = 23400
# The window must contain at least this many complete regular sessions after now.
MIN_SESSIONS_COVERED = 2
# The DNA should outlive the window by this many sessions (a WARN, not a BLOCK).
DNA_MARGIN_SESSIONS = 2
# A single steady-state order above this share of the principal is refused in
# production. A mis-read position (holdings=0) otherwise buys the whole principal.
STEADY_NOTIONAL_FRACTION_MAX = Decimal("0.25")
# The t0 funding order is ~FIX_C, so the funding release may go up to this.
FUNDING_NOTIONAL_FRACTION_MAX = Decimal("1.25")
# execution_service.DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS (100) as a fraction: the
# dispatch quote may differ this much from the decision quote and still be sent.
MAX_DISPATCH_DRIFT = Decimal("0.01")
# A cap below this multiple of DIFF would block ordinary rebalances.
NOTIONAL_FLOOR_DIFF_MULTIPLE = Decimal("3")
RECOMMENDED_NOTIONAL_FRACTION = {"UAT": Decimal("0.15"), "PROD": Decimal("0.10")}
FUNDING_NOTIONAL_FRACTION = Decimal("1.25")
# Share-count cap = notional / reference price * this margin (secondary guard).
QUANTITY_MARGIN = Decimal("1.3")
FUNDING_SESSION_ORDERS = 2
PROD_CANARY_SESSION_ORDERS = 10
FUNDING_MODES = ("prefunded", "initial-funding")


def slots_per_full_session(interval_seconds: int) -> int:
    return math.ceil(FULL_SESSION_SECONDS / int(interval_seconds))


def format_window_end(moment: datetime) -> str:
    """Canonical LEGO_TRADING_WINDOW_END spelling (the binding hashes the text)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_end_after_sessions(now: datetime, sessions: int) -> datetime:
    """Close of the *sessions*-th complete regular session after *now* (UTC)."""
    if type(sessions) is not int or sessions < 1:
        raise ValueError("sessions must be a positive integer")
    seen = 0
    for opened, closed in regular_sessions(now):
        if opened >= now:
            seen += 1
            if seen == sessions:
                return closed
    raise ValueError("calendar horizon exceeded")


def _calendar_key() -> str:
    # operational_health.dna_end ignores this value except as its cache key; it
    # must change whenever the holiday inputs of market_clock change.
    return "|".join((CALENDAR_RULES_VERSION,
                     os.environ.get("LEGO_MARKET_HOLIDAYS", ""),
                     os.environ.get("LEGO_MARKET_EARLY_CLOSES", "")))


def dna_horizon(dna_code: str, origin_utc: str | None, interval_seconds: int) -> dict:
    length = len(decode_dna(dna_code))
    if not origin_utc:
        return {"length": length, "end_utc": None}
    end = dna_end(origin_utc, length, int(interval_seconds), _calendar_key())
    return {"length": length, "end_utc": end}


def _parse_limits(values):
    try:
        return ExecutionLimits.parse(tuple(values)), None
    except ExecutionLimitError as exc:
        return None, str(exc)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _nice_ceiling(value: Decimal) -> int:
    """Round up to a number a human would type: 1s <10, 5s <100, 50s above."""
    whole = int(value.to_integral_value(rounding=ROUND_CEILING))
    step = 1 if whole < 10 else 5 if whole < 100 else 50
    return int(math.ceil(whole / step) * step)


@dataclass(frozen=True)
class ReleaseInputs:
    environment: str
    mode: str
    active: bool
    symbol: str
    principal: Decimal
    diff: Decimal
    limits: tuple
    allow_fractional: bool
    dna_code: str
    dna_interval: int
    dna_origin: str | None
    initial_funding: bool = False
    reference_price: Decimal | None = None

    @property
    def trading(self) -> bool:
        return self.mode == "trade" and self.active

    @property
    def production(self) -> bool:
        return self.environment == "PROD"


def inputs_from_runtime(runtime, *, initial_funding: bool = False,
                        reference_price=None) -> ReleaseInputs:
    """Duck-typed on config.RuntimeConfig so this module never imports config."""
    operator, deployment = runtime.operator, runtime.deployment
    bundle = operator.dna_bundle
    return ReleaseInputs(
        environment=deployment.environment, mode=operator.mode, active=operator.active,
        symbol=operator.symbol,
        principal=Decimal(str(operator.principal_usd)), diff=Decimal(str(operator.diff_usd)),
        limits=tuple(deployment.execution_limits),
        allow_fractional=deployment.allow_fractional,
        dna_code=bundle.dna_code, dna_interval=int(bundle.interval_seconds),
        dna_origin=bundle.origin_utc, initial_funding=initial_funding,
        reference_price=(None if reference_price is None
                         else Decimal(str(reference_price))))


def findings(inp: ReleaseInputs, *, now: datetime | None = None) -> list[dict]:
    """Static findings, plus the time-dependent ones when *now* is supplied."""
    out: list[dict] = []
    # What a failed check costs: refused for a deployment that can trade,
    # a warning otherwise (observe/inactive deployments are not enforced).
    fail = BLOCK if inp.trading else WARN

    def add(id_: str, severity: str, message: str, *, static: bool = True) -> None:
        out.append({"id": id_, "severity": severity, "message": message,
                    "static": static})

    limits, reason = _parse_limits(inp.limits)
    if limits is None:
        add("limits_configured", fail if inp.trading else INFO,
            f"execution limits unusable ({reason}); every new order is blocked")
    else:
        add("limits_configured", INFO, "all four execution limits parse")
        _limit_findings(inp, limits, add, fail)

    if inp.production and inp.allow_fractional:
        add("fractional_policy", WARN,
            "production validation should start with whole shares "
            "(LEGO_ALLOW_FRACTIONAL=false); review any change explicitly")

    horizon = None
    try:
        horizon = dna_horizon(inp.dna_code, inp.dna_origin, inp.dna_interval)
    except (ValueError, KeyError) as exc:
        add("dna_covers_window", fail, f"DNA horizon cannot be computed: {exc}")
    dna_end_at = None
    if horizon is not None:
        if horizon["end_utc"] is None:
            add("dna_covers_window", WARN,
                "DNA bundle has no origin_utc, so its end date cannot be assessed")
        else:
            dna_end_at = datetime.fromisoformat(horizon["end_utc"])
    if dna_end_at is not None and limits is not None:
        if dna_end_at < limits.end:
            add("dna_covers_window", fail,
                f"DNA ends {format_window_end(dna_end_at)} before the approval window "
                f"ends {format_window_end(limits.end)}; trading would stop at the DNA end")
        elif complete_sessions_between(limits.end, dna_end_at) < DNA_MARGIN_SESSIONS:
            add("dna_covers_window", WARN,
                f"DNA ends {format_window_end(dna_end_at)}, fewer than "
                f"{DNA_MARGIN_SESSIONS} sessions after the window; prepare the next DNA")
        else:
            add("dna_covers_window", INFO,
                f"DNA ends {format_window_end(dna_end_at)}, after the window")

    if now is not None:
        if limits is not None:
            if now >= limits.end:
                add("window_open", fail,
                    f"approval window ended {format_window_end(limits.end)}; "
                    "every order is blocked until a new release is deployed",
                    static=False)
            else:
                sessions = complete_sessions_between(now, limits.end)
                if sessions < MIN_SESSIONS_COVERED:
                    add("window_sessions", fail,
                        f"window covers only {sessions} complete regular session(s) "
                        f"after now; need at least {MIN_SESSIONS_COVERED}", static=False)
                else:
                    add("window_sessions", INFO,
                        f"window covers {sessions} complete regular session(s)",
                        static=False)
        if dna_end_at is not None and now >= dna_end_at:
            add("dna_open", fail,
                f"DNA ended {format_window_end(dna_end_at)}; no further decisions",
                static=False)
    return out


def _limit_findings(inp: ReleaseInputs, limits: ExecutionLimits, add, fail) -> None:
    production = inp.production
    ratio = limits.notional / inp.principal
    pct = f"{ratio * 100:.0f}%"
    if inp.initial_funding:
        need = inp.principal * (1 + MAX_DISPATCH_DRIFT)
        if limits.notional < need:
            add("funding_notional_sufficient", fail,
                f"t0 funding order is ~principal ({_plain(inp.principal)}) and may be "
                f"sent up to 1% above its decision price, so the notional cap must be "
                f"at least {_plain(need)}; cap {_plain(limits.notional)} would block it")
        if ratio > FUNDING_NOTIONAL_FRACTION_MAX:
            add("notional_cap_ratio", fail if production else WARN,
                f"funding notional cap is {pct} of principal; the funding release "
                f"allows at most {int(FUNDING_NOTIONAL_FRACTION_MAX * 100)}%")
        if inp.reference_price is not None:
            needed_qty = inp.principal / inp.reference_price
            if limits.quantity < needed_qty:
                add("funding_quantity_sufficient", fail,
                    f"t0 funding order is ~{needed_qty:.0f} shares at the reference "
                    f"price; quantity cap {_plain(limits.quantity)} would block it")
        add("funding_release_temporary", WARN,
            "initial-funding caps are loose by design: after the funding fill is "
            "confirmed, deploy a new release with steady caps")
        if limits.orders > FUNDING_SESSION_ORDERS + 1:
            add("session_orders_cap", WARN,
                f"funding release allows {limits.orders} orders per session; "
                f"{FUNDING_SESSION_ORDERS} is enough to fund and confirm")
        return
    if ratio > STEADY_NOTIONAL_FRACTION_MAX:
        add("notional_cap_ratio", fail if production else WARN,
            f"notional cap {_plain(limits.notional)} is {pct} of principal "
            f"{_plain(inp.principal)}; above 25% a mis-read position (holdings=0) "
            "could buy the whole principal in one order")
    else:
        add("notional_cap_ratio", INFO, f"notional cap is {pct} of principal")
    floor = inp.diff * NOTIONAL_FLOOR_DIFF_MULTIPLE
    if limits.notional < floor:
        add("notional_cap_floor", WARN,
            f"notional cap {_plain(limits.notional)} is below 3x DIFF ({_plain(floor)}); "
            "ordinary rebalances would be blocked")
    slots = slots_per_full_session(inp.dna_interval)
    if limits.orders > slots:
        add("session_orders_cap", fail if production else WARN,
            f"max session orders {limits.orders} exceeds the {slots} slots in a "
            "session (one order per slot), so the cap can never bind")
    else:
        add("session_orders_cap", INFO,
            f"max session orders {limits.orders} of {slots} slots per session")


def recommend_limits(principal, reference_price=None, *, profile: str = "UAT",
                     interval_seconds: int = 900,
                     initial_funding: bool = False) -> dict:
    """Principal-proportional caps (see docs/AUDIT_20261005_TH.md).

    notional = principal x (15% UAT | 10% PROD canary); the t0 funding release
    uses 125%. Quantity is a secondary guard derived from a reference price.
    """
    if profile not in RECOMMENDED_NOTIONAL_FRACTION:
        raise ValueError("profile must be UAT or PROD")
    principal = Decimal(str(principal))
    slots = slots_per_full_session(interval_seconds)
    if initial_funding:
        basis = principal * FUNDING_NOTIONAL_FRACTION
        orders = FUNDING_SESSION_ORDERS
    else:
        basis = principal * RECOMMENDED_NOTIONAL_FRACTION[profile]
        orders = slots if profile == "UAT" else min(slots, PROD_CANARY_SESSION_ORDERS)
    result = {
        "profile": profile, "initial_funding": initial_funding,
        "LEGO_MAX_ORDER_NOTIONAL_USD": str(_nice_ceiling(basis)),
        "LEGO_MAX_SESSION_ORDERS": str(orders),
    }
    if reference_price is not None:
        price = Decimal(str(reference_price))
        if price <= 0:
            raise ValueError("reference price must be positive")
        qty_basis = basis if initial_funding else basis * QUANTITY_MARGIN
        result["LEGO_MAX_ORDER_QUANTITY"] = str(_nice_ceiling(qty_basis / price))
    return result


def assess(inp: ReleaseInputs, *, now: datetime | None = None) -> dict:
    items = findings(inp, now=now)
    limits, _ = _parse_limits(inp.limits)
    result = {
        "ok": not any(f["severity"] == BLOCK for f in items),
        "trading": inp.trading, "environment": inp.environment,
        "blocking": [f["id"] for f in items if f["severity"] == BLOCK],
        "warnings": [f["id"] for f in items if f["severity"] == WARN],
        "findings": items,
    }
    if limits is not None:
        result["limits"] = {
            "quantity": _plain(limits.quantity), "notional_usd": _plain(limits.notional),
            "session_orders": limits.orders,
            "notional_pct_of_principal": float(limits.notional / inp.principal * 100),
            "window_end_utc": format_window_end(limits.end)}
        if now is not None:
            result["limits"]["seconds_to_window_end"] = (limits.end - now).total_seconds()
            result["limits"]["complete_sessions_in_window"] = (
                complete_sessions_between(now, limits.end) if limits.end > now else 0)
    try:
        result["dna"] = dna_horizon(inp.dna_code, inp.dna_origin, inp.dna_interval)
    except (ValueError, KeyError):
        result["dna"] = {"length": None, "end_utc": None}
    result["recommended"] = recommend_limits(
        inp.principal, inp.reference_price,
        profile="PROD" if inp.production else "UAT",
        interval_seconds=inp.dna_interval, initial_funding=inp.initial_funding)
    return result


def prod_live_ack(inp: ReleaseInputs, binding: str, *, funding: str) -> str | None:
    """The string a production deployment must echo in LEGO_PROD_LIVE_ACK.

    None when the release is not production/trading or fails a *static* check, so
    an unfit release can never produce a matching acknowledgement. It carries the
    numbers a human is approving (symbol, caps, window end, funding mode) plus the
    first 16 hex of the release binding, so an old acknowledgement cannot be reused
    for another release. It is a review checkpoint, not a cryptographic signature:
    the release binding it embeds is itself a plain fingerprint.
    """
    if funding not in FUNDING_MODES:
        raise ValueError("funding must be prefunded or initial-funding")
    if not (inp.production and inp.trading) or len(str(binding)) < 16:
        return None
    static = findings(replace(inp, initial_funding=(funding == "initial-funding")))
    if any(f["severity"] == BLOCK for f in static):
        return None
    limits, _ = _parse_limits(inp.limits)
    if limits is None:
        return None
    return "-".join((
        "LIVE", "PROD", inp.symbol, f"q{_plain(limits.quantity)}",
        f"n{_plain(limits.notional)}", f"o{limits.orders}",
        limits.end.astimezone(UTC).strftime("%Y%m%dT%H%MZ"), funding,
        str(binding)[:16]))


def prod_live_acks(inp: ReleaseInputs, binding: str) -> dict:
    return {mode: prod_live_ack(inp, binding, funding=mode) for mode in FUNDING_MODES}


def prod_live_gate_open(runtime) -> bool:
    """Runtime check behind RuntimeConfig.prod_live_gate_open. Never raises."""
    try:
        supplied = str(getattr(runtime.deployment, "prod_live_ack", "") or "")
        if not supplied:
            return False
        inp = inputs_from_runtime(runtime)
        binding = runtime.deployment.expected_release_binding
        for expected in prod_live_acks(inp, binding).values():
            if expected and hmac.compare_digest(supplied.encode(), expected.encode()):
                return True
    except Exception:  # noqa: BLE001 - a gate that raises would fail open
        return False
    return False
