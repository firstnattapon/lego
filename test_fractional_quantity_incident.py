"""The 2026-10-05 UBER SELL: a 5-place order the broker echoed at 2 places.

Order f327df93... (chain UBER_33b541c2034c) sent quantity 1.53175. Webull's Order
Detail answered total_quantity 1.530000, so order_recovery.validate_evidence
raised BrokerContractAnomaly, the intent went to MANUAL_RECONCILIATION_REQUIRED
and the account/symbol fence stayed shut. 24 Sep had the same shape
(0.31721 -> 0.32). The instrument profile states no precision, so the five places
came from a constant webull_io assumed; the default is now two.

These pin what that change has to keep true together: an order is built at the
places the broker echoes, a chain bound to the old five-place contract can move
to it (and only in that direction), and evidence that disagrees with the
submitted payload is still refused. The fixtures are the incident's own values.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

import pytest

import lego_orders as orders
import main
import order_recovery as recovery
from conftest import FAKE_DB
from lego_one_row import (PASS_MIN_ORDER, READY_BUY, READY_SELL, Config,
                          build_decision, compute_row)
from lego_state import (STATE_PATH, RuntimeIdentityError,
                        _capability_is_tightening, chain_key, commit_final_row,
                        read_anchor)
from webull_io import (DEFAULT_FRACTIONAL_DECIMAL_PLACES,
                       FRACTIONAL_QUANTITY_INCREMENT,
                       MAX_FRACTIONAL_DECIMAL_PLACES, FractionalGateError,
                       InstrumentCapability, WebullConfigError,
                       build_order_payload, evaluate_fractional_order_gate,
                       parse_instrument_capability)

UTC = timezone.utc
RUN_ID = "f327df93cec10d5180b7d9db6255759f"
BROKER_ORDER_ID = "0388C9VQEK80O0KF4Q5C000000"
# What the incident's chain state held, exactly as RTDB returned it.
STORED_FIVE_PLACES = {"quantity_increment": "1.0000000000000001e-05",
                      "decimal_precision": 5}
STORED_TWO_PLACES = {"quantity_increment": "0.01", "decimal_precision": 2}


@pytest.fixture(autouse=True)
def clean_db():
    FAKE_DB.store.clear()


def _uber(places: int, step: float) -> Config:
    return Config(symbol="UBER", fix_c=10000.0, diff=25.0, dna_code="bypass:100",
                  strategy_id="shannon_demon_lego_v2",
                  decimal_precision=places, quantity_increment=step)


def _profile(**extra) -> dict:
    item = {"symbol": "UBER", "status": "OC", "currency": "USD",
            "category": "US_STOCK", "lot_size": 1, "fractionable": True}
    item.update(extra)
    return {"data": [item]}


def _capability(**changes) -> InstrumentCapability:
    return InstrumentCapability(
        symbol="UBER", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=True, **changes)


# --- the contract a profile without a stated precision resolves to ---------------

def test_profile_without_a_stated_precision_defaults_to_two_places():
    capability = parse_instrument_capability(_profile(), "UBER")

    assert capability.decimal_precision == 2
    assert capability.quantity_increment == Decimal("0.01")
    assert capability.minimum_quantity_if_authoritative == Decimal("0.01")
    assert DEFAULT_FRACTIONAL_DECIMAL_PLACES == 2
    assert FRACTIONAL_QUANTITY_INCREMENT == Decimal("0.01")


def test_five_place_contract_from_older_data_still_loads_under_the_ceiling():
    legacy = _capability(quantity_increment=Decimal("0.00001"), decimal_precision=5)

    assert (legacy.decimal_precision, legacy.quantity_increment) == (
        5, Decimal("0.00001"))
    assert legacy.minimum_quantity_if_authoritative == Decimal("0.00001")
    assert MAX_FRACTIONAL_DECIMAL_PLACES == 5
    with pytest.raises(WebullConfigError, match="between 0 and 5"):
        _capability(decimal_precision=MAX_FRACTIONAL_DECIMAL_PLACES + 1)


@pytest.mark.parametrize("increment,places", [
    ("0.001", 3), ("0.00001", 5), ("0.5", 1), ("1", 0)])
def test_a_stated_increment_fixes_the_places_instead_of_the_default(
        increment, places):
    """An increment finer than the places would build payloads no intent equals."""
    capability = parse_instrument_capability(
        _profile(fractional_increment=increment), "UBER")

    assert capability.quantity_increment == Decimal(increment)
    assert capability.decimal_precision == places


@pytest.mark.parametrize("increment", [
    "NaN", "Infinity", "-Infinity", "0", "-0.01"])
def test_an_unusable_increment_is_still_a_config_error(increment):
    """Deriving the places from it must not turn this into a TypeError."""
    with pytest.raises(WebullConfigError, match="positive finite"):
        _capability(quantity_increment=Decimal(increment))


def test_a_stated_precision_wins_over_the_increment():
    capability = _capability(quantity_increment=Decimal("0.00001"),
                             decimal_precision=3)
    assert capability.decimal_precision == 3


# --- the order is built at the places the broker echoes ---------------------------

def test_sell_payload_is_built_at_two_places_and_never_rounds_up():
    two = _uber(2, 0.01)

    assert build_order_payload(two, "SELL", 1.53175, RUN_ID)[0]["quantity"] == "1.53"
    assert build_order_payload(two, "SELL", 1.539, RUN_ID)[0]["quantity"] == "1.53"
    # What the engine commits is what execution_service demands the payload equal.
    committed = 1.53
    assert Decimal(build_order_payload(two, "SELL", committed, RUN_ID)[0]["quantity"]
                   ) == Decimal(str(committed))


def test_engine_sizes_the_incident_decision_at_two_places():
    five = _uber(5, 0.00001)
    two = replace(five, decimal_precision=2, quantity_increment=0.01)

    before = build_decision(five, 68.68, 147.13455, 1)
    after = build_decision(two, 68.68, 147.13455, 1)

    assert (before.status, before.quantity) == (READY_SELL, 1.53175)   # what was sent
    assert (after.status, after.quantity) == (READY_SELL, 1.53)
    assert after.quantity <= float(Decimal("147.13455").quantize(
        Decimal("0.01"), rounding=ROUND_DOWN))


@pytest.mark.parametrize("holdings", [147.13455, 12.34567, 3.00001, 0.99999])
@pytest.mark.parametrize("price", [68.68, 331.2, 2.5])
@pytest.mark.parametrize("factor", [0.9, 1.1])
def test_every_engine_quantity_has_at_most_two_places_and_a_sell_fits_holdings(
        holdings, price, factor):
    cfg = replace(_uber(2, 0.01), fix_c=holdings * price * factor, diff=0.0)

    decision = build_decision(cfg, price, holdings, 1)

    if decision.status == PASS_MIN_ORDER:
        return                                   # below the USD 1 floor: no order
    assert decision.status in (READY_BUY, READY_SELL)
    quantity = Decimal(str(decision.quantity))
    assert quantity == quantity.quantize(Decimal("0.01"))
    assert decision.quantity > 0
    if decision.status == READY_SELL:
        assert quantity <= Decimal(str(holdings)).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN)


# --- the gate judges the quantity against the capability's own places -------------

def test_gate_refuses_more_places_than_the_capability_has():
    gate = dict(side="SELL", price="68.68", holdings="147.13455",
                order_type="MARKET", session_check_fn=lambda at: True)
    default = _capability()                                       # two places
    five = _capability(quantity_increment=Decimal("0.00001"), decimal_precision=5)

    evaluate_fractional_order_gate(capability=default, quantity="1.53", **gate)
    with pytest.raises(FractionalGateError, match="2 decimal places"):
        evaluate_fractional_order_gate(capability=default, quantity="1.53175", **gate)
    evaluate_fractional_order_gate(capability=five, quantity="1.53175", **gate)


# --- order evidence is still compared with the submitted payload ------------------

def _order_detail(total="1.530000", filled="0.000000", status="PENDING") -> dict:
    """The shape recorded in the incident's reconcile_evidence."""
    return {
        "client_order_id": RUN_ID, "combo_type": "NORMAL",
        "combo_order_id": BROKER_ORDER_ID,
        "orders": [{
            "symbol": "UBER", "side": "SELL", "status": status, "fees": [],
            "commission": {"actual_commission": "0", "receivable_commission": "0"},
            "client_order_id": RUN_ID, "order_type": "MARKET",
            "instrument_type": "EQUITY", "order_id": BROKER_ORDER_ID,
            "support_trading_session": "CORE", "entrust_type": "QTY",
            "total_quantity": total, "filled_quantity": filled,
            "place_time": "2026-10-05T13:45:29.066Z",
            "place_time_at": "2026-10-05T13:45:29.066Z",
            "filled_price": "0.000000", "time_in_force": "DAY",
        }],
    }


def _intent(quantity: str) -> dict:
    return {"run_id": RUN_ID, "symbol": "UBER", "side": "SELL",
            "quantity": quantity, "broker_order_id": BROKER_ORDER_ID,
            "order_payload": [{"client_order_id": RUN_ID, "symbol": "UBER",
                               "side": "SELL", "quantity": quantity}]}


def test_the_incident_evidence_is_still_an_anomaly_for_the_five_place_payload():
    detail = _order_detail()
    with pytest.raises(orders.BrokerContractAnomaly, match="1.530000"):
        recovery.validate_evidence(
            _intent("1.53175"), detail, orders.summarize_order_result({}, detail))


@pytest.mark.parametrize("status,filled", [
    ("PENDING", "0.000000"), ("FILLED", "1.530000"),
    ("PARTIAL_FILLED", "0.700000"), ("CANCELLED", "0.000000")])
def test_the_incident_evidence_validates_for_the_two_place_payload(status, filled):
    detail = _order_detail(filled=filled, status=status)

    evidence = recovery.validate_evidence(
        _intent("1.53"), detail, orders.summarize_order_result({}, detail))

    assert evidence.total_quantity == Decimal("1.53")
    assert evidence.filled_quantity == Decimal(filled)


def test_a_broker_total_that_differs_from_the_payload_stays_fail_closed():
    detail = _order_detail(total="1.520000")
    with pytest.raises(orders.BrokerContractAnomaly):
        recovery.validate_evidence(
            _intent("1.53"), detail, orders.summarize_order_result({}, detail))


# --- a chain bound to five places can move to two, and only that way ---------------

SLOT_1, SLOT_2, SLOT_3, SLOT_4 = (
    f"2026-10-05T{moment}Z" for moment in ("13:30:18", "13:45:14", "14:00:11", "14:15:09"))


def _commit(cfg: Config, captured_at: str) -> dict:
    snapshot = {"captured_at": captured_at, "price": 68.68, "holdings": 147.13455}
    anchor = read_anchor(cfg)
    return commit_final_row(cfg, snapshot, anchor, compute_row(cfg, snapshot, anchor))


def _state(cfg: Config) -> dict:
    return FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()


def test_the_five_place_chain_rebinds_to_two_places_once_and_says_so():
    old, new = _uber(5, 0.00001), _uber(2, 0.01)
    assert chain_key(old) == chain_key(new)
    _commit(old, SLOT_1)
    assert _state(old)["instrument_capability"] == STORED_FIVE_PLACES

    migrated = _commit(new, SLOT_2)

    assert migrated["committed"] is True
    assert migrated["instrument_capability_migrated_from"] == STORED_FIVE_PLACES
    assert _state(new)["instrument_capability"] == STORED_TWO_PLACES
    assert _state(new)["version"] == 2
    again = _commit(new, SLOT_3)
    assert again["committed"] is True
    assert "instrument_capability_migrated_from" not in again


def test_the_chain_cannot_be_widened_back_to_five_places():
    old, new = _uber(5, 0.00001), _uber(2, 0.01)
    _commit(old, SLOT_1)
    _commit(new, SLOT_2)

    with pytest.raises(RuntimeIdentityError, match="lot-size migration"):
        _commit(old, SLOT_3)

    assert _state(new)["instrument_capability"] == STORED_TWO_PLACES
    assert _state(new)["version"] == 2                       # pointer untouched


def test_a_narrower_precision_with_an_unaligned_increment_is_refused():
    old = _uber(5, 0.00001)
    unaligned = _uber(3, 0.000015)
    _commit(old, SLOT_1)

    with pytest.raises(RuntimeIdentityError, match="lot-size migration"):
        _commit(unaligned, SLOT_2)

    assert _state(old)["instrument_capability"] == STORED_FIVE_PLACES


def test_an_unchanged_contract_is_no_migration():
    """The same contract is no migration: nothing is announced, nothing refused."""
    cfg = _uber(2, 0.01)
    _commit(cfg, SLOT_1)
    second = _commit(cfg, SLOT_2)
    assert second["committed"] is True
    assert "instrument_capability_migrated_from" not in second


@pytest.mark.parametrize("prior,current,expected", [
    # The incident's stored strings: 0.01 / 1.0000000000000001e-05 is not 1000.
    (STORED_FIVE_PLACES, STORED_TWO_PLACES, True),
    # Whole shares (ALLOW_FRACTIONAL=false) narrows the same way from either.
    (STORED_FIVE_PLACES, {"quantity_increment": "1", "decimal_precision": 0}, True),
    (STORED_TWO_PLACES, {"quantity_increment": "1", "decimal_precision": 0}, True),
    ({"quantity_increment": "0.001", "decimal_precision": 3}, STORED_TWO_PLACES,
     True),
    # Widening, or no narrower: the chain was not bound to this.
    (STORED_TWO_PLACES, STORED_FIVE_PLACES, False),
    (STORED_TWO_PLACES, STORED_TWO_PLACES, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "0.01", "decimal_precision": 5}, False),
    # Narrower places, but the new step is not a whole number of old steps.
    (STORED_FIVE_PLACES,
     {"quantity_increment": "1.5000000000000001e-05", "decimal_precision": 3},
     False),
    # Anything unreadable is not a tightening.
    (STORED_FIVE_PLACES, {}, False),
    (STORED_FIVE_PLACES, None, False),
    (None, STORED_TWO_PLACES, False),
    ("not a contract", STORED_TWO_PLACES, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "0.01", "decimal_precision": "2"}, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "0.01", "decimal_precision": True}, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "nan", "decimal_precision": 2}, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "inf", "decimal_precision": 2}, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "0", "decimal_precision": 2}, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "-0.01", "decimal_precision": 2}, False),
    (STORED_FIVE_PLACES,
     {"quantity_increment": "garbage", "decimal_precision": 2}, False),
    ({"quantity_increment": "0", "decimal_precision": 5}, STORED_TWO_PLACES, False),
])
def test_tightening_is_decided_on_the_decimal_not_the_stored_float(
        prior, current, expected):
    assert _capability_is_tightening(prior, current) is expected


# --- the tick reports the rebind in the response Scheduler already calls ----------

TICK_ENV = {
    "LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
    "LEGO_DNA_CODE": "bypass:100", "LEGO_STRATEGY_ID": "shannon_demon_lego_v2",
    "LEGO_SLOT_SECONDS": "1800", "LEGO_DNA_ORIGIN_UTC": "2026-10-05T13:30:00Z",
    "LEGO_DNA_CLOCK_MODE": "market", "FIREBASE_DB_URL": "https://x.firebaseio.com",
}


def _tick(monkeypatch, moment: datetime, places: str):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment

    for key, value in {**TICK_ENV, "LEGO_DECIMAL_PRECISION": places}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTO_SUBMIT", raising=False)
    monkeypatch.delenv("LEGO_INLINE_ORDER_WORKER", raising=False)
    monkeypatch.setattr(main, "datetime", _Now)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": 68.68, "holdings": 147.13455})
    return main.lego_one_row(object())


def test_the_tick_reports_the_rebind_once_and_stops_on_a_widening(monkeypatch):
    first, code = _tick(monkeypatch, datetime(2026, 10, 5, 13, 45, 5, tzinfo=UTC), "5")
    assert code == 200 and first["committed"] is True
    assert "instrument_capability_migrated_from" not in first

    rebound, code = _tick(monkeypatch, datetime(2026, 10, 5, 14, 15, 5, tzinfo=UTC), "2")
    assert code == 200 and rebound["committed"] is True
    assert rebound["instrument_capability_migrated_from"] == {
        "quantity_increment": "1", "decimal_precision": 5}

    settled, code = _tick(monkeypatch, datetime(2026, 10, 5, 14, 45, 5, tzinfo=UTC), "2")
    assert code == 200 and settled["committed"] is True
    assert "instrument_capability_migrated_from" not in settled

    widened, code = _tick(monkeypatch, datetime(2026, 10, 5, 15, 15, 5, tzinfo=UTC), "5")
    assert code == 500
    assert widened["status"] == "CONFIG_ERROR" and widened["committed"] is False
    assert "lot-size migration" in widened["error"]
