"""End-to-end wiring of the lego_one_row handler: clock -> row -> slot guard.

Broker access is stubbed; the point is that DNA time advances on its own and
that slot provenance reaches Firebase.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from conftest import FAKE_DB
from lego_outbox import OUTBOX_PATH, list_actionable, row_is_committed
from lego_state import STATE_PATH, chain_key

UTC = timezone.utc
SESSION_OPEN_SLOT = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)     # ordinal 0 on a 30m grid


def _fixed_now(moment: datetime):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return _Now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FAKE_DB.store.clear()
    for key, value in {
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "3000", "LEGO_DIFF": "5",
        "LEGO_DNA_CODE": "bypass:100", "LEGO_DECIMAL_PRECISION": "2",
        "LEGO_SLOT_SECONDS": "1800", "LEGO_DNA_ORIGIN_UTC": "2026-07-23T18:00:00Z",
        "LEGO_DNA_CLOCK_MODE": "market", "FIREBASE_DB_URL": "https://x.firebaseio.com",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTO_SUBMIT", raising=False)
    monkeypatch.delenv("LEGO_INLINE_ORDER_WORKER", raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    # A healthy token by default: the tests below own the warnings node and
    # a real token file does not exist in a test process.
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})


def _run(monkeypatch, moment: datetime, price: float, holdings: float = 9.0):
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def test_row_commits_with_slot_provenance(monkeypatch):
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200
    assert body["committed"] is True
    assert body["pipeline_status"] == "ROW_COMMITTED"
    assert (body["step"], body["market_step"], body["market_slot_id"]) == (0, 0, "2026-07-23:9")
    assert body["clock_mode"] == "market"
    doc = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert doc["market_ordinal"] == 0 and doc["market_slot_id"] == "2026-07-23:9"
    assert "order_worker" not in body      # inline dispatch is off by default


def test_scheduler_retry_in_same_slot_does_not_double_commit(monkeypatch):
    first, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 12, 41, tzinfo=UTC), 320.9)
    assert code == 200
    assert body["committed"] is False
    assert body["pipeline_status"] == "SLOT_CONSUMED"
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}").get()
    assert state["version"] == 1 and state["last_run_id"] == first["run_id"]


def test_dna_jumps_when_scheduler_misses_slots(monkeypatch):
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    # 18:30 and 19:00 never fire; the 19:30 slot is ordinal 3.
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 19, 30, 5, tzinfo=UTC), 322.0)
    assert code == 200
    assert body["committed"] is True
    assert (body["step"], body["market_step"]) == (3, 3)
    assert body["legacy_step"] == 1 and body["alignment_error"] == -2


def test_calendar_change_fails_closed(monkeypatch):
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    monkeypatch.setenv("LEGO_MARKET_HOLIDAYS", "2026-07-22")
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC), 321.0)
    assert code == 409
    assert body["pipeline_status"] == "CALENDAR_DRIFT"
    assert body["committed"] is False


def test_untrained_slot_size_is_a_config_error(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "600")
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 500 and body["pipeline_status"] == "CONFIG_ERROR"


def test_market_holiday_is_closed_even_before_the_clock_resolves(monkeypatch):
    """One calendar: a declared holiday blocks the row, degraded clock or not."""
    monkeypatch.setenv("LEGO_MARKET_HOLIDAYS", "2026-07-23")
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "shadow")
    monkeypatch.delenv("LEGO_DNA_ORIGIN_UTC")          # force the degraded path
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200 and body["pipeline_status"] == "MARKET_CLOSED"
    assert FAKE_DB.reference("webull_lego_rows").get() is None


def test_ordinal_regression_fails_closed(monkeypatch):
    _run(monkeypatch, datetime(2026, 7, 23, 19, 30, 5, tzinfo=UTC), 320.0)   # ordinal 3
    state_ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}")
    committed = state_ref.get()["version"]
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 321.0)                 # ordinal 0
    assert code == 409 and body["pipeline_status"] == "ORDINAL_REGRESSION"
    assert body["committed"] is False
    assert state_ref.get()["version"] == committed                           # pointer intact


# --- outbox is written only after the slot is safely committed ---------------

@pytest.fixture
def auto_submit(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    monkeypatch.setenv("WEBULL_ENV", "UAT")


def test_intent_uses_the_committed_run_id(monkeypatch, auto_submit):
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200 and body["status"] == "READY_BUY"
    ck = chain_key(main.load_config())
    intents = list_actionable(ck)
    assert [i["run_id"] for i in intents] == [body["run_id"]]
    assert intents[0]["client_order_id"] == body["run_id"]
    assert len(body["run_id"]) <= 32                 # Webull client_order_id limit
    assert row_is_committed(body["run_id"]) is True


def test_outbox_failure_never_reports_the_row_as_uncommitted(monkeypatch, auto_submit):
    """The slot is durable before the outbox is touched, so an intent that fails
    to write costs this row its order and nothing else."""
    def boom(*args, **kwargs):
        raise RuntimeError("RTDB write failed")
    monkeypatch.setattr(main, "put_intent", boom)

    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200
    assert body["committed"] is True
    assert body["pipeline_status"] == "ROW_COMMITTED"
    assert "RTDB write failed" in body["outbox_error"]
    row = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert row["committed"] is True
    assert FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}").get()["version"] == 1


def test_unsupported_clock_mode_is_a_config_error(monkeypatch):
    """Same family as an untrained slot size: a deploy typo, not an engine fault."""
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "turbo")
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 500 and body["pipeline_status"] == "CONFIG_ERROR"
    assert FAKE_DB.reference("webull_lego_rows").get() is None


@pytest.mark.parametrize("category", ["US_STONK", "US_OPTION", "US_CRYPTO",
                                      "US_FUTURES", "HK_STOCK", "CN_STOCK"])
def test_unsupported_market_category_is_a_config_error_before_broker(monkeypatch,
                                                                     category):
    """Snapshot category and hard-coded US EQUITY order payload must agree."""
    touched = []
    monkeypatch.setattr(
        main, "build_clients", lambda: touched.append(True) or (object(), object()))
    monkeypatch.setenv("LEGO_MARKET_CATEGORY", category)
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 500 and body["pipeline_status"] == "CONFIG_ERROR"
    assert FAKE_DB.reference("webull_lego_rows").get() is None
    assert touched == []


def test_an_unhealthy_token_is_reported_without_stopping_the_row(monkeypatch):
    """Days of warning beat a dead chain: the row still commits."""
    monkeypatch.setattr(main, "token_health", lambda: {
        "ok": False, "reasons": ["token เหลืออีก 0.50 วันก่อนหมดอายุ"],
        "token_dir": "/tmp/webull_token", "days_left": 0.5, "expires_at": None})
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200 and body["committed"] is True
    assert "หมดอายุ" in body["token_warning"]
    warning = FAKE_DB.reference("webull_lego_warnings/webull_token").get()
    assert warning["count"] == 1 and warning["days_left"] == 0.5
    assert "expires_at" not in warning        # None is not written to RTDB


def test_clock_mode_tolerates_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "  Market ")
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200 and body["clock_mode"] == "market"
    assert body["step"] == body["market_step"] == 0


def test_rejected_commit_leaves_no_intent_behind(monkeypatch, auto_submit):
    first, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 12, 41, tzinfo=UTC), 320.9)
    assert code == 200 and body["pipeline_status"] == "SLOT_CONSUMED"
    ck = chain_key(main.load_config())
    assert list(FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}").get()) == [first["run_id"]]


def test_order_worker_failure_never_blocks_the_row(monkeypatch):
    monkeypatch.setenv("LEGO_INLINE_ORDER_WORKER", "true")
    monkeypatch.setattr(main, "_run_order_worker",
                        lambda cfg, limit=1: (_ for _ in ()).throw(RuntimeError("broker down")))
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200
    assert body["committed"] is True
    assert "broker down" in body["order_worker"]["error"]


# --- execution-leg safety: the order that never resolves ---------------------

def _stub_broker(monkeypatch, *, place=None, detail=None, holdings_after=None):
    monkeypatch.setattr(main, "preview_market_order", lambda tc, o: True)
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "place_market_order",
                        place or (lambda tc, o: {"order_status": "FILLED"}))
    monkeypatch.setattr(main, "fetch_order_detail", detail or (lambda tc, r: {}))
    # The post-execution position read the worker uses to confirm a fill really
    # moved the account before it books ΔAₙ/Aₙ/Eₙ.
    monkeypatch.setattr(main, "fetch_holdings",
                        lambda tc, cfg: 0.0 if holdings_after is None
                        else float(holdings_after))


def _reject(*args, **kwargs):
    raise RuntimeError("insufficient buying power")


def test_unresolvable_order_stops_being_retried(monkeypatch, auto_submit):
    """PLACING_UNKNOWN must be bounded: the broker answers UNKNOWN forever when
    it never accepted the order, and nothing else expires that state."""
    monkeypatch.setenv("LEGO_RECONCILE_MAX_ATTEMPTS", "3")
    _stub_broker(monkeypatch, place=_reject)
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    ck = chain_key(main.load_config())

    seen = [main._run_order_worker(main.load_config(), limit=3)["results"][0]["status"]
            for _ in range(3)]
    assert seen == ["PLACING_UNKNOWN", "PLACING_UNKNOWN", "RECONCILE_ABANDONED"]

    assert list_actionable(ck) == []                      # drops out of the queue
    audit = FAKE_DB.reference(f"webull_lego_order_audit/{body['run_id']}").get()
    assert audit["needs_manual_check"] is True            # but not out of sight
    assert "insufficient buying power" in audit["first_error"]   # why it started
    assert "still UNKNOWN" in audit["last_error"]                # why it gave up


def test_a_manual_reconcile_fence_blocks_a_later_decision(monkeypatch, auto_submit):
    """Queue churn stops, but broker uncertainty must not permit another order.

    RECONCILE_ABANDONED means "ask a human", not "the broker rejected it". The
    DNA may keep committing decisions, while the money path stays fenced until
    that first run is reconciled explicitly.
    """
    monkeypatch.setenv("LEGO_RECONCILE_MAX_ATTEMPTS", "2")
    _stub_broker(monkeypatch, place=_reject)
    cfg = main.load_config()
    ck = chain_key(cfg)
    first, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    for _ in range(2):
        main._run_order_worker(cfg, limit=1)

    # Even if the broker endpoint recovers, a later run cannot be sent while the
    # abandoned run's existence remains unanswered.
    placed = []
    _stub_broker(monkeypatch, place=lambda tc, order: placed.append(order) or {
        "order_status": "FILLED"}, detail=lambda tc, r: {
        "order_status": "FILLED", "filled_quantity": 1.0, "avg_filled_price": 322.0},
        holdings_after=10.0)
    later, _ = _run(monkeypatch, datetime(2026, 7, 23, 19, 30, 5, tzinfo=UTC), 322.0)
    result = main._run_order_worker(cfg, limit=1)
    assert result["dispatch_blocked"] is True
    assert result["dispatch_inflight_run_id"] == first["run_id"]
    assert placed == []
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{ck}/{later['run_id']}").get()["status"] == "PENDING_DISPATCH"


def test_whole_share_quantity_is_not_truncated(monkeypatch, auto_submit):
    """LEGO_DECIMAL_PRECISION=0 is documented as valid; 20 shares must not
    reach the broker as '2'."""
    monkeypatch.setenv("LEGO_DECIMAL_PRECISION", "0")
    monkeypatch.setenv("LEGO_FIX_C", "2000")
    sent = []
    _stub_broker(monkeypatch, place=lambda tc, o: sent.append(o) or {"order_status": "FILLED"},
                 detail=lambda tc, r: {"order_status": "FILLED"})
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 100.0, holdings=0.0)
    assert body["status"] == "READY_BUY"
    main._run_order_worker(main.load_config(), limit=1)
    assert sent and sent[0][0]["quantity"] == "20"


# --- a snapshot that loses the position must never become an order -----------

def test_vanished_holdings_fails_closed(monkeypatch):
    """gap = fix_c is the largest order the strategy can make; it must not come
    from a positions response that simply forgot the symbol."""
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0, holdings=9.0)
    state_ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}")
    before = state_ref.get()["version"]

    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC),
                      320.0, holdings=0.0)
    assert code == 409 and body["pipeline_status"] == "HOLDINGS_ANOMALY"
    assert body["committed"] is False
    assert state_ref.get()["version"] == before        # pointer + prev_holdings intact
    assert state_ref.get()["prev_holdings"] == 9.0


def test_zero_holdings_never_compounds_across_slots(monkeypatch, auto_submit):
    """The damage was never one bad order: a broker that keeps answering 0 used
    to buy fix_c again every slot until buying power ran out."""
    placed = []
    monkeypatch.setattr(main, "preview_market_order", lambda tc, o: True)
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "fetch_order_detail", lambda tc, r: {"order_status": "FILLED"})
    monkeypatch.setattr(main, "place_market_order",
                        lambda tc, o: placed.append(o[0]["quantity"]) or {"order_status": "FILLED"})
    _run(monkeypatch, SESSION_OPEN_SLOT, 100.0, holdings=15.0)
    main._run_order_worker(main.load_config(), limit=3)
    placed.clear()

    # 18:30, 19:00, 19:30 — the rest of this session, all reading a lost position
    for i in (1, 2, 3):
        body, code = _run(monkeypatch,
                          SESSION_OPEN_SLOT + timedelta(minutes=30 * i), 100.0, holdings=0.0)
        assert code == 409 and body["pipeline_status"] == "HOLDINGS_ANOMALY"
        main._run_order_worker(main.load_config(), limit=3)
    assert placed == []


def test_genesis_and_legacy_state_are_not_blocked(monkeypatch):
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0, holdings=0.0)
    assert code == 200 and body["committed"] is True       # genesis has no reference

    ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}")
    state = ref.get()
    state.pop("prev_holdings")                             # state written before the field
    ref.set(state)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC),
                      321.0, holdings=0.0)
    assert code == 200 and body["committed"] is True


def test_flat_chain_stays_flat_without_complaining(monkeypatch):
    """prev_holdings = 0 -> 0 is not an anomaly, it is an unchanged position."""
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0, holdings=0.0)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC),
                      321.0, holdings=0.0)
    assert code == 200 and body["committed"] is True


def test_partial_drop_is_ordinary_and_allowed(monkeypatch):
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0, holdings=9.0)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC),
                      321.0, holdings=4.0)
    assert code == 200 and body["committed"] is True


def test_operator_can_acknowledge_a_real_liquidation(monkeypatch):
    monkeypatch.setenv("LEGO_ALLOW_ZERO_HOLDINGS", "true")
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0, holdings=9.0)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC),
                      321.0, holdings=0.0)
    assert code == 200 and body["committed"] is True


# --- the gate array is now guarded like the calendar ------------------------

def test_same_dna_code_decoding_differently_fails_closed(monkeypatch):
    """numpy documents no stream guarantee for Generator, so the decoded array
    can change while dna_code and config_hash stay identical."""
    import lego_state
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    state_ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}")
    assert state_ref.get()["dna_fingerprint"]
    before = state_ref.get()["version"]

    monkeypatch.setattr(lego_state, "dna_fingerprint", lambda code: "different0000000")
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC), 321.0)
    assert code == 409 and body["pipeline_status"] == "DNA_DRIFT"
    assert body["committed"] is False
    assert state_ref.get()["version"] == before
