"""The one-off funding release before the QBTS canary (audit 2026-11-03, funding addendum).

The owner's account holds no real QBTS yet, and LEAPS 100 make the first decision a BUY of
FIX_C - 100 x P (about 491 USD at 10.09), more than the canary caps (150 USD / 20 shares)
carry. Step A deploys the existing initial-funding release (caps up to 125 % of FIX_C, two
orders per session, the shortest window release_horizon accepts) so the system buys it; step B,
after the fill is confirmed, is the prefunded canary on the same chain.

Covered here: the recommended funding caps of a principal whose 125 % is not a "nice" number
(1500 -> 1875, not 1900: the plan used to block itself), the preflight's funding judgement,
the runtime gate, the live wrapper in both steps, and a whole funding-then-canary session on the
real-money path against a broker double.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import release_horizon as rh
from config import load_runtime_config
from operational_health import complete_sessions_between
from test_audit_20261103 import (PREFLIGHT_OK, acked, authorized, deployed_line, exact_env,
                                 log, run_live, sandbox)  # noqa: F401  (sandbox: fixture)
from test_dispatch_overshoot import isolate  # noqa: F401  (autouse)
from test_paper_mode import paper_values
from test_prod_live_preflight import equity, facts, finding, option, params
from test_prod_live_trade_session import PROD_DB, TradeSession, audited, path, trade_values
from tools import prod_live_preflight as pf

PRICE = "10.09"                                  # QBTS at the owner's first live run (03 Nov)
FUNDING_CAPS = {"LEGO_MAX_ORDER_NOTIONAL_USD": "1875", "LEGO_MAX_ORDER_QUANTITY": "200",
                "LEGO_MAX_SESSION_ORDERS": "2"}


# ------------------------------------------------------------------ the recommended funding caps
def test_the_recommended_funding_caps_of_1500_stay_inside_125_percent():
    caps = rh.recommend_limits(Decimal("1500"), Decimal(PRICE), profile="PROD", initial_funding=True)
    assert {k: caps[k] for k in FUNDING_CAPS} == FUNDING_CAPS


@pytest.mark.parametrize("principal", ["1500", "1001", "3000", "10000", "2222.22"])
def test_the_recommended_funding_release_never_blocks_its_own_ratio(principal):
    caps = rh.recommend_limits(Decimal(principal), Decimal(PRICE), profile="PROD", initial_funding=True)
    notional = Decimal(caps["LEGO_MAX_ORDER_NOTIONAL_USD"])
    assert notional / Decimal(principal) <= rh.FUNDING_NOTIONAL_FRACTION_MAX
    assert notional >= Decimal(principal) * (1 + rh.MAX_DISPATCH_DRIFT)   # the funding BUY fits


def test_the_steady_caps_are_unchanged():
    assert rh.recommend_limits(Decimal("1500"), Decimal("10.82"), profile="PROD") == {
        "profile": "PROD", "initial_funding": False, "LEGO_MAX_ORDER_NOTIONAL_USD": "150",
        "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_MAX_ORDER_QUANTITY": "20"}
    assert rh.recommend_limits(10000, "67.4", profile="PROD", initial_funding=True)[
        "LEGO_MAX_ORDER_NOTIONAL_USD"] == "12500"


# ------------------------------------------------------------------ the preflight's judgement
def flat(positions=None, **overrides):
    """No real QBTS; the 100 LEAPS held (or, with positions=[], confirmed by the owner)."""
    return facts(positions=[option()] if positions is None else positions,
                 quote={"price": PRICE, "quote_time": None}, **overrides)


def test_a_flat_account_passes_the_funding_preflight_with_one_buy_of_about_491():
    report = pf.judge(flat(), params(funding="initial-funding"))
    assert report["ok"] is True, report["findings"]
    derived = report["derived"]
    assert derived["funding"] == "initial-funding"
    assert derived["first_order"] == {"side": "BUY", "notional_usd": "491.00", "quantity": "48.66"}
    assert derived["caps"] == FUNDING_CAPS
    assert "funding caps" in finding(report, "first_order")["message"]
    assert "funding_not_needed" not in {f["id"] for f in report["findings"]}


def test_the_owners_first_run_passes_as_funding_with_the_leaps_confirmed():
    """03 Nov: no option row, no real shares; LEGO_LEAPS_CONFIRMED=yes and funding."""
    report = pf.judge(flat(positions=[]), params(funding="initial-funding", leaps_confirmed=True))
    assert report["ok"] is True, report["findings"]
    assert finding(report, "leaps_held")["severity"] == "WARN"


@pytest.mark.parametrize("positions,price,why", [
    ([equity("48.66"), option()], PRICE, "within DIFF"),        # already funded
    ([equity("34"), option()], PRICE, "fits the canary caps"),    # 1500 - 134 x 10.09 = 147.94
    ([equity("120"), option()], PRICE, "SELL"),                   # too many shares
    ([option()], "14.60", "within DIFF"),                         # 1460 - 1500: no BUY band
])
def test_the_funding_release_is_refused_when_the_canary_can_or_must_do_it(positions, price, why):
    report = pf.judge(facts(positions=positions, quote={"price": price}), params(funding="initial-funding"))
    assert "funding_not_needed" in report["blocking"]
    assert why in finding(report, "funding_not_needed")["message"]


def test_the_prefunded_preflight_is_unchanged_and_names_the_funding_release():
    report = pf.judge(flat(), params())
    assert report["blocking"] == ["first_order"]
    assert report["derived"]["caps"]["LEGO_MAX_ORDER_NOTIONAL_USD"] == "150"
    message = finding(report, "first_order")["message"]
    assert "hold between 34 and 63" in message and "LEGO_FUNDING_MODE_OVERRIDE=initial-funding" in message
    # A first SELL beyond the caps is not offered the funding release.
    sell = pf.judge(facts(positions=[equity("120"), option()]), params())
    assert "initial-funding" not in finding(sell, "first_order")["message"]


def test_an_unknown_funding_mode_blocks():
    assert pf.judge(flat(), params(funding="loose"))["blocking"] == ["arguments"]


def test_the_cli_passes_the_funding_mode(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(pf, "run", lambda p: seen.setdefault("params", p) and pf._report({}, [], {}))
    argv = ["--symbol", "QBTS", "--fix-c", "1500", "--diff", "50", "--leaps", "100",
            "--allow-fractional", "true", "--window-sessions", "2"]
    assert pf.main(argv + ["--funding", "initial-funding"]) == 0
    assert seen["params"].funding == "initial-funding"
    capsys.readouterr()
    with pytest.raises(SystemExit):
        pf.main(argv + ["--funding", "loose"])


# ------------------------------------------------------------------ the runtime gate
def funding_env(tmp_path, **overrides):
    window = rh.format_window_end(rh.window_end_after_sessions(datetime.now(timezone.utc), 2))
    return authorized(exact_env(tmp_path, LEGO_TRADING_WINDOW_END=window, **FUNDING_CAPS, **overrides))


def test_only_the_funding_acknowledgement_opens_a_release_with_funding_caps(tmp_path):
    values = funding_env(tmp_path)
    runtime = load_runtime_config(values)
    acks = rh.prod_live_acks(rh.inputs_from_runtime(runtime), runtime.deployment.expected_release_binding)
    assert acks["prefunded"] is None                       # 125 % caps are no steady release
    assert re.fullmatch(r"LIVE-PROD-QBTS-q200-n1875-o2-\d{8}T\d{4}Z-initial-funding-[0-9a-f]{16}",
                        acks["initial-funding"])
    opened = load_runtime_config(acked(values, funding="initial-funding"))
    assert opened.prod_live_gate_open is True and opened.allows_new_broker_mutation is True
    canary_ack = acked(authorized(exact_env(tmp_path)))["LEGO_PROD_LIVE_ACK"]
    assert load_runtime_config({**values, "LEGO_PROD_LIVE_ACK": canary_ack}).prod_live_gate_open is False


def test_the_funding_acknowledgement_does_not_open_the_canary(tmp_path):
    funding_ack = acked(funding_env(tmp_path), funding="initial-funding")["LEGO_PROD_LIVE_ACK"]
    canary = authorized(exact_env(tmp_path))
    assert load_runtime_config({**canary, "LEGO_PROD_LIVE_ACK": funding_ack}).prod_live_gate_open is False


# ------------------------------------------------------------------ the live wrapper, steps A and B
FUNDING_PREFLIGHT = {**PREFLIGHT_OK, "warnings": ["leaps_held"], "derived": {
    **PREFLIGHT_OK["derived"], "reference_price": PRICE, "real_shares": "0", "leaps_held": "0",
    "real_target": "48.66", "funding": "initial-funding",
    "first_order": {"side": "BUY", "notional_usd": "491.00", "quantity": "48.66"}}}
FUNDING = {"LEGO_FUNDING_MODE_OVERRIDE": "initial-funding", "LEGO_LEAPS_CONFIRMED": "yes"}
FUNDING_ACK = re.compile(
    r"LEGO_PROD_LIVE_ACK=(LIVE-PROD-QBTS-q200-n1875-o2-\d{8}T\d{4}Z-initial-funding-[0-9a-f]{16})")


def preflight_call(sandbox):
    return next(line for line in log(sandbox).splitlines() if "tools/prod_live_preflight.py" in line)


def test_step_a_stops_at_the_funding_acknowledgement(sandbox):
    result = run_live(sandbox, FUNDING, preflight=FUNDING_PREFLIGHT)
    assert result.returncode == 3, result.stdout[-3000:] + result.stderr
    assert FUNDING_ACK.search(result.stdout), result.stdout[-3000:]
    assert not re.search(r"LEGO_PROD_LIVE_ACK=\S*-prefunded-", result.stdout)  # no steady ack here
    assert "functions deploy" not in log(sandbox)
    call = preflight_call(sandbox)
    assert "--window-sessions 2 --funding initial-funding" in call and "--leaps-confirmed" in call
    for text in ("one-off FUNDING: caps up to 1875 USD per order; expect one BUY of about 491.00 USD",
                 "200 / 1875 USD / 2 per session", "owner-confirmed (LEGO_LEAPS_CONFIRMED=yes), NOT seen",
                 "WITHOUT LEGO_FUNDING_MODE_OVERRIDE", "ops.py halt-orders"):
        assert text in result.stdout, text


def test_step_a_deploys_the_funding_release_for_two_sessions(sandbox):
    started = datetime.now(timezone.utc)
    ack = FUNDING_ACK.search(run_live(sandbox, FUNDING, preflight=FUNDING_PREFLIGHT).stdout).group(1)
    result = run_live(sandbox, {**FUNDING, "LEGO_PROD_LIVE_ACK": ack}, preflight=FUNDING_PREFLIGHT)
    assert result.returncode == 78, result.stdout[-3000:] + result.stderr   # stub stops at deploy
    assert "PROD live acknowledgement verified" in result.stdout
    assert "Funding mode   : initial-funding" in result.stdout
    deployed = deployed_line(sandbox)
    for value in ("LEGO_SYMBOL=QBTS,LEGO_FIX_C=1500,", "LEGO_DIFF=50,", "LEGO_LEAPS=100,",
                  "LEGO_MODE=trade,LEGO_ACTIVE=true,",
                  "LEGO_MAX_ORDER_QUANTITY=200,LEGO_MAX_ORDER_NOTIONAL_USD=1875,LEGO_MAX_SESSION_ORDERS=2,",
                  f"LEGO_PROD_LIVE_ACK={ack}", f"FIREBASE_DB_URL={PROD_DB},"):
        assert value in deployed, value
    end = datetime.strptime(re.search(r"LEGO_TRADING_WINDOW_END=([0-9TZ:-]+)", deployed).group(1),
                            "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert complete_sessions_between(started, end) == 2


def test_step_b_is_the_canary_once_the_shares_are_held(sandbox):
    result = run_live(sandbox, {"LEGO_LEAPS_CONFIRMED": "yes"})          # PREFLIGHT_OK: 38 real
    assert result.returncode == 3, result.stdout[-3000:] + result.stderr
    assert re.search(r"LIVE-PROD-QBTS-q20-n150-o10-\d{8}T\d{4}Z-prefunded-", result.stdout)
    assert "--window-sessions 5 --funding prefunded" in preflight_call(sandbox)
    assert "one-off FUNDING" not in result.stdout and "WITHOUT LEGO_FUNDING_MODE_OVERRIDE" not in result.stdout


def test_a_funding_ack_cannot_deploy_the_canary(sandbox):
    ack = FUNDING_ACK.search(run_live(sandbox, FUNDING, preflight=FUNDING_PREFLIGHT).stdout).group(1)
    result = run_live(sandbox, {"LEGO_LEAPS_CONFIRMED": "yes", "LEGO_PROD_LIVE_ACK": ack})
    assert result.returncode == 1 and "does not match this release" in result.stderr
    assert "functions deploy" not in log(sandbox)


# ------------------------------------------------------------------ a whole funding, then canary
def funding_values():
    values = paper_values(
        LEGO_SYMBOL="QBTS", LEGO_FIX_C="1500", LEGO_DIFF="50", LEGO_LEAPS="100",
        LEGO_MODE="trade", LEGO_ACTIVE="true", LEGO_ALLOW_FRACTIONAL="true",
        FIREBASE_DB_URL=PROD_DB, **FUNDING_CAPS)
    runtime = load_runtime_config(values)
    acks = rh.prod_live_acks(rh.inputs_from_runtime(runtime), runtime.deployment.expected_release_binding)
    assert acks["prefunded"] is None and acks["initial-funding"]
    return {**values, "LEGO_PROD_LIVE_ACK": acks["initial-funding"]}


def test_the_funding_release_buys_once_then_the_canary_trades_the_same_chain(monkeypatch):
    funding = TradeSession(monkeypatch, real="0", price=PRICE, values=funding_values())
    funding.run([PRICE] * 4)
    rows, intents, state = audited(funding)
    assert funding.violations == []
    assert [(p["side"], p["quantity"]) for p in funding.placed] == [("BUY", Decimal("48.66"))]
    assert funding.placed[0]["notional"] < Decimal("1875")
    assert funding.real == Decimal("48.66")
    assert rows[0]["คำสั่ง"] == "TRIGGER_ACTION" and rows[0]["จำนวนถือครอง (หุ้น)"] == 100.0  # 0 + LEAPS
    assert [r["สถานะ"] for r in rows[1:]] == ["PASS_THRESHOLD"] * 3          # funded: within DIFF
    assert '"funding_baseline_pending": true' not in json.dumps(state)      # LEAPS: not a flat chain

    canary = TradeSession(monkeypatch, real=str(funding.real), values=trade_values())
    assert canary.ck == funding.ck                                          # caps are not the chain
    prices = path(PRICE, "+0-0-0+0+")[1:]
    for index, price in enumerate(prices, start=4):
        canary.decide(index, price), canary.dispatch()
    rows, intents, state = audited(canary)
    assert canary.violations == []
    assert canary.placed and all(p["notional"] <= Decimal("150") for p in canary.placed)
    assert len(rows) == 4 + len(prices)
    assert all(Decimal(str(r["จำนวนถือครอง (หุ้น)"])) > 100 for r in rows[4:])  # real shares never 0
