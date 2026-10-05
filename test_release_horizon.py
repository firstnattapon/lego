"""Release horizon / limits assessment and the production acknowledgement."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import release_horizon as rh
from market_clock import session_slot_count

NOW = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)          # Monday, before the open
ORIGIN = "2026-09-08T13:30:00Z"
BINDING = "ab" * 32


def window(sessions):
    return rh.format_window_end(rh.window_end_after_sessions(NOW, sessions))


def make(**overrides):
    base = dict(
        environment="UAT", mode="trade", active=True, symbol="UBER",
        principal=Decimal("10000"), diff=Decimal("25"),
        limits=("30", "1500", "26", window(10)), allow_fractional=True,
        dna_code="bypass:6500", dna_interval=900, dna_origin=ORIGIN)
    base.update(overrides)
    return rh.ReleaseInputs(**base)


def prod(**overrides):
    return make(**{"environment": "PROD", "allow_fractional": False,
                   "limits": ("20", "1000", "10", window(5)), **overrides})


def ids(result, key="blocking"):
    return set(result[key])


# --------------------------------------------------------------- calendar helpers
def test_slots_per_session_matches_the_market_clock():
    assert rh.slots_per_full_session(900) == session_slot_count(date(2026, 10, 7), 900) == 26
    assert rh.slots_per_full_session(3600) == session_slot_count(date(2026, 10, 7), 3600) == 7


def test_drift_constant_matches_the_dispatch_gate():
    import execution_service
    assert rh.MAX_DISPATCH_DRIFT == (
        Decimal(str(execution_service.DEFAULT_MAX_DISPATCH_PRICE_DRIFT_BPS)) / 10000)


def test_window_end_is_the_close_of_the_nth_complete_session():
    assert rh.format_window_end(rh.window_end_after_sessions(NOW, 1)) == "2026-10-05T20:00:00Z"
    assert window(10) == "2026-10-16T20:00:00Z"                     # Oct 12 is a trading day
    # Mid-session: the session in progress is not complete, so it is not counted.
    midday = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    assert rh.format_window_end(rh.window_end_after_sessions(midday, 1)) == "2026-10-06T20:00:00Z"
    # Weekend is skipped.
    friday_night = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
    assert rh.format_window_end(rh.window_end_after_sessions(friday_night, 1)) == "2026-10-12T20:00:00Z"


def test_window_end_follows_the_new_york_dst_change():
    before = datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)      # Fri; DST ends Sun Nov 1
    assert rh.format_window_end(rh.window_end_after_sessions(before, 1)) == "2026-10-30T20:00:00Z"
    assert rh.format_window_end(rh.window_end_after_sessions(before, 2)) == "2026-11-02T21:00:00Z"


@pytest.mark.parametrize("sessions", [1, 2, 5, 10, 25])
def test_sessions_between_agrees_with_window_end(sessions):
    end = rh.window_end_after_sessions(NOW, sessions)
    assert rh.complete_sessions_between(NOW, end) == sessions


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True])
def test_window_end_rejects_non_positive_or_non_integer_sessions(bad):
    with pytest.raises(ValueError):
        rh.window_end_after_sessions(NOW, bad)


# ------------------------------------------------------------------ the incident
def test_incident_release_is_blocked_and_its_caps_are_flagged():
    incident = make(limits=("1000", "36000", "30", "2026-10-01T06:19:26Z"),
                    dna_code="bypass:500")
    result = rh.assess(incident, now=NOW)
    assert not result["ok"]
    assert ids(result) == {"window_open"}
    assert {"notional_cap_ratio", "session_orders_cap"} <= ids(result, "warnings")
    assert result["limits"]["notional_pct_of_principal"] == 360.0
    # Expiry is time-dependent: a static assessment cannot know it.
    assert rh.assess(incident)["ok"]


def test_a_new_window_longer_than_the_dna_is_blocked_statically():
    release = make(dna_code="bypass:500")                          # DNA ends 2026-10-05T15:00Z
    result = rh.assess(release)
    assert ids(result) == {"dna_covers_window"}
    blocker = next(f for f in result["findings"] if f["severity"] == rh.BLOCK)
    assert "2026-10-05T15:00:00Z" in blocker["message"]


def test_proposed_uat_profile_has_no_blockers_or_warnings():
    result = rh.assess(make(), now=NOW)
    assert result["ok"] and not result["blocking"] and not result["warnings"]
    assert result["limits"]["complete_sessions_in_window"] == 10
    assert result["dna"]["end_utc"].startswith("2027-09-07T19:30:00")


def test_window_must_cover_two_complete_sessions():
    one = make(limits=("30", "1500", "26", window(1)))
    assert ids(rh.assess(one, now=NOW)) == {"window_sessions"}
    assert rh.assess(make(limits=("30", "1500", "26", window(2))), now=NOW)["ok"]


def test_an_exhausted_dna_blocks():
    result = rh.assess(make(dna_code="bypass:500"),
                       now=datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc))
    assert {"dna_open", "dna_covers_window"} <= ids(result)


def test_dna_without_origin_cannot_be_assessed_but_is_not_a_blocker():
    result = rh.assess(make(dna_origin=None), now=NOW)
    assert result["ok"] and "dna_covers_window" in ids(result, "warnings")


@pytest.mark.parametrize("length,verdict", [
    (520, "blocked"),    # ordinals 494..519 are 2026-10-05: ends before the window (10-06)
    (572, "warned"),     # ends 2026-10-07 20:00Z: one session after the window
    (598, "clear")])     # ends 2026-10-08 20:00Z: two sessions after the window
def test_dna_must_outlive_the_window_by_two_sessions(length, verdict):
    result = rh.assess(make(dna_code=f"bypass:{length}",
                            limits=("30", "1500", "26", window(2))))
    assert ("dna_covers_window" in ids(result)) is (verdict == "blocked")
    assert ("dna_covers_window" in ids(result, "warnings")) is (verdict == "warned")


def test_observe_inactive_is_never_enforced():
    incident = make(mode="observe", active=False,
                    limits=("1000", "36000", "30", "2026-10-01T06:19:26Z"))
    result = rh.assess(incident, now=NOW)
    assert result["ok"] and not result["blocking"]
    assert "window_open" in ids(result, "warnings")


def test_unusable_limits_block_trading_but_only_inform_observe():
    unusable = make(limits=("", "", "", ""))
    assert ids(rh.assess(unusable)) == {"limits_configured"}
    observe = rh.assess(replace(unusable, mode="observe", active=False))
    assert observe["ok"] and not observe["warnings"]


# --------------------------------------------------- caps against the principal
@pytest.mark.parametrize("notional,blocked", [("1500", False), ("2500", False),
                                              ("2501", True), ("36000", True)])
def test_production_notional_cap_is_at_most_a_quarter_of_principal(notional, blocked):
    result = rh.assess(prod(limits=("20", notional, "10", window(5))))
    assert ("notional_cap_ratio" in ids(result)) is blocked


def test_uat_loose_notional_cap_only_warns():
    result = rh.assess(make(limits=("30", "36000", "26", window(10))))
    assert result["ok"] and "notional_cap_ratio" in ids(result, "warnings")


def test_session_orders_above_the_slot_count_can_never_bind():
    assert "session_orders_cap" in ids(rh.assess(prod(limits=("20", "1000", "27", window(5)))))
    assert "session_orders_cap" in ids(rh.assess(make(limits=("30", "1500", "27", window(10)))),
                                       "warnings")
    equal = rh.assess(make(limits=("30", "1500", "26", window(10))))
    assert "session_orders_cap" not in ids(equal) | ids(equal, "warnings")


def test_notional_cap_below_three_times_diff_warns():
    result = rh.assess(make(limits=("30", "50", "26", window(10))))
    assert "notional_cap_floor" in ids(result, "warnings")


def test_production_with_fractional_shares_warns():
    result = rh.assess(prod(allow_fractional=True))
    assert result["ok"] and "fractional_policy" in ids(result, "warnings")
    assert "fractional_policy" not in ids(rh.assess(prod()), "warnings")


# ------------------------------------------------------ t0 funding (order ~ FIX_C)
@pytest.mark.parametrize("notional,blocked", [
    ("1500", True),       # steady caps cannot fund: the t0 order is ~10,000
    ("10000", True),      # exactly the principal is not enough: dispatch may be 1% higher
    ("10099", True),
    ("10100", False),
    ("12500", False)])
def test_funding_cap_must_cover_the_principal_plus_dispatch_drift(notional, blocked):
    result = rh.assess(prod(initial_funding=True, limits=("200", notional, "2", window(2))))
    assert ("funding_notional_sufficient" in ids(result)) is blocked


def test_funding_cap_above_125_percent_is_blocked_in_production_and_warned_in_uat():
    limits = ("200", "13000", "2", window(2))
    assert "notional_cap_ratio" in ids(rh.assess(prod(initial_funding=True, limits=limits)))
    uat = rh.assess(make(initial_funding=True, limits=limits))
    assert "notional_cap_ratio" in ids(uat, "warnings") and uat["ok"]


def test_funding_quantity_cap_is_checked_against_the_reference_price():
    base = dict(initial_funding=True, reference_price=Decimal("67.4"))
    short = rh.assess(prod(limits=("100", "12500", "2", window(2)), **base))
    assert "funding_quantity_sufficient" in ids(short)                # needs ~148 shares
    enough = rh.assess(prod(limits=("200", "12500", "2", window(2)), **base))
    assert "funding_quantity_sufficient" not in ids(enough)


def test_funding_release_is_always_flagged_as_temporary():
    result = rh.assess(prod(initial_funding=True, limits=("200", "12500", "2", window(2))))
    assert result["ok"] and "funding_release_temporary" in ids(result, "warnings")
    many = rh.assess(prod(initial_funding=True, limits=("200", "12500", "20", window(2))))
    assert "session_orders_cap" in ids(many, "warnings")


# ------------------------------------------------------------------ recommendations
def test_recommended_limits_for_a_ten_thousand_principal():
    assert rh.recommend_limits(10000, "67.4") == {
        "profile": "UAT", "initial_funding": False, "LEGO_MAX_ORDER_NOTIONAL_USD": "1500",
        "LEGO_MAX_SESSION_ORDERS": "26", "LEGO_MAX_ORDER_QUANTITY": "30"}
    assert rh.recommend_limits(10000, "67.4", profile="PROD") == {
        "profile": "PROD", "initial_funding": False, "LEGO_MAX_ORDER_NOTIONAL_USD": "1000",
        "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_MAX_ORDER_QUANTITY": "20"}
    assert rh.recommend_limits(10000, "67.4", profile="PROD", initial_funding=True) == {
        "profile": "PROD", "initial_funding": True, "LEGO_MAX_ORDER_NOTIONAL_USD": "12500",
        "LEGO_MAX_SESSION_ORDERS": "2", "LEGO_MAX_ORDER_QUANTITY": "200"}


def test_recommendation_without_a_reference_price_omits_the_quantity_cap():
    assert "LEGO_MAX_ORDER_QUANTITY" not in rh.recommend_limits(10000)


def test_recommended_values_satisfy_the_assessment():
    for profile, builder in (("UAT", make), ("PROD", prod)):
        rec = rh.recommend_limits(10000, "67.4", profile=profile)
        limits = (rec["LEGO_MAX_ORDER_QUANTITY"], rec["LEGO_MAX_ORDER_NOTIONAL_USD"],
                  rec["LEGO_MAX_SESSION_ORDERS"], window(5))
        result = rh.assess(builder(limits=limits), now=NOW)
        assert result["ok"], result["blocking"]
    funding = rh.recommend_limits(10000, "67.4", profile="PROD", initial_funding=True)
    limits = (funding["LEGO_MAX_ORDER_QUANTITY"], funding["LEGO_MAX_ORDER_NOTIONAL_USD"],
              funding["LEGO_MAX_SESSION_ORDERS"], window(2))
    assert rh.assess(prod(initial_funding=True, limits=limits,
                          reference_price=Decimal("67.4")), now=NOW)["ok"]


def test_recommendation_rejects_bad_inputs():
    with pytest.raises(ValueError):
        rh.recommend_limits(10000, "67.4", profile="STAGING")
    with pytest.raises(ValueError):
        rh.recommend_limits(10000, "0")


# ------------------------------------------------------------ the production ack
def test_ack_carries_the_numbers_being_approved():
    ack = rh.prod_live_ack(prod(), BINDING, funding="prefunded")
    assert window(5) == "2026-10-09T20:00:00Z"
    assert ack == "LIVE-PROD-UBER-q20-n1000-o10-20261009T2000Z-prefunded-" + BINDING[:16]
    assert rh.prod_live_ack(prod(), BINDING, funding="prefunded") == ack      # deterministic


def test_ack_changes_with_the_release_it_approves():
    base = rh.prod_live_ack(prod(), BINDING, funding="prefunded")
    assert rh.prod_live_ack(prod(), "cd" * 32, funding="prefunded") != base
    assert rh.prod_live_ack(prod(limits=("20", "1000", "10", window(6))), BINDING,
                            funding="prefunded") != base
    assert rh.prod_live_ack(prod(symbol="AAPL"), BINDING, funding="prefunded") != base


def test_funding_release_has_its_own_ack_and_prefunded_ack_is_refused_for_loose_caps():
    loose = prod(limits=("200", "12500", "2", window(2)))
    assert rh.prod_live_ack(loose, BINDING, funding="prefunded") is None
    funding = rh.prod_live_ack(loose, BINDING, funding="initial-funding")
    assert funding and "-initial-funding-" in funding
    assert set(rh.prod_live_acks(loose, BINDING)) == {"prefunded", "initial-funding"}


@pytest.mark.parametrize("inp", [
    make(),                                                         # UAT needs no ack
    prod(mode="observe", active=False),
    prod(limits=("20", "3000", "10", window(5))),                   # cap above 25% of principal
    prod(limits=("20", "1000", "27", window(5))),                   # cap cannot bind
    prod(dna_code="bypass:500"),                                    # DNA ends before the window
    prod(limits=("", "", "", ""))])
def test_unfit_or_non_production_releases_have_no_ack(inp):
    assert rh.prod_live_ack(inp, BINDING, funding="prefunded") is None


def test_ack_ignores_time_dependent_findings():
    expired = prod(limits=("20", "1000", "10", "2026-10-01T06:19:26Z"))
    assert rh.prod_live_ack(expired, BINDING, funding="prefunded") is not None


def test_unknown_funding_mode_is_an_error():
    with pytest.raises(ValueError):
        rh.prod_live_ack(prod(), BINDING, funding="maybe")
