"""Regression tests for the final money-path and identity review."""
from __future__ import annotations

import threading
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import lego_archive
import lego_outbox
import lego_state
import main
import webull_io
from conftest import FAKE_DB, FakeReference
from lego_one_row import compute_row
from lego_outbox import (OUTBOX_PATH, begin_place_attempt, claim_intent,
                         put_intent, release_intent_claim, update_intent)
from lego_state import (REALIZED_PATH, STATE_PATH, RuntimeIdentityMismatch,
                        apply_realized_fill, chain_key, commit_final_row,
                        read_anchor)


UTC = timezone.utc
SLOT = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)


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
        "LEGO_SYMBOL": "AAPL",
        "LEGO_FIX_C": "3000",
        "LEGO_DIFF": "5",
        "LEGO_DNA_CODE": "bypass:100",
        "LEGO_DECIMAL_PRECISION": "2",
        "LEGO_SLOT_SECONDS": "1800",
        "LEGO_DNA_ORIGIN_UTC": "2026-07-23T18:00:00Z",
        "LEGO_DNA_CLOCK_MODE": "market",
        "FIREBASE_DB_URL": "https://x.firebaseio.com",
        "WEBULL_ENV": "UAT",
        "WEBULL_ACCOUNT_ID": "review-account-a",
        # Historical fixtures use a fixed July timestamp. Individual freshness
        # tests override this; unrelated money-fence tests are not age tests.
        "LEGO_MAX_DISPATCH_QUOTE_AGE_SECONDS": "1000000000",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("AUTO_SUBMIT", "LEGO_INLINE_ORDER_WORKER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(
        main, "token_health", lambda: {"ok": True, "ready": True, "reasons": []})


def _run_row(monkeypatch, *, price=320.0, holdings=9.0):
    monkeypatch.setattr(main, "datetime", _fixed_now(SLOT))
    monkeypatch.setattr(main, "fetch_snapshot", lambda _t, _d, _cfg: {
        "captured_at": SLOT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": SLOT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price,
        "holdings": holdings,
    })
    return main.lego_one_row(object())


def test_invalid_environment_is_config_error_and_never_reads_broker(monkeypatch):
    monkeypatch.setenv("WEBULL_ENV", "STAGING")
    touched = []
    monkeypatch.setattr(
        main, "build_clients", lambda: touched.append(True) or (object(), object()))

    body, code = _run_row(monkeypatch)

    assert code == 500
    assert body["status"] == body["pipeline_status"] == "CONFIG_ERROR"
    assert touched == []
    assert FAKE_DB.reference("webull_lego_rows").get() is None


def _pending_buy_intent(cfg, run_id: str) -> dict:
    decision = main.build_decision(cfg, price=320.0, holdings=9.0, signal=1)
    return {
        "status": "PENDING_DISPATCH",
        "row_status": decision.status,
        "side": decision.side,
        "quantity": decision.quantity,
        "symbol": cfg.symbol,
        "step": 0,
        "signal": 1,
        "decision_price": 320.0,
        "decision_holdings": 9.0,
        "decision_time": "2026-07-23T18:00:05Z",
        "created_at": "2026-07-23T18:00:05Z",
        "expires_at": "2099-07-23T18:30:00Z",
        "run_id": run_id,
        "chain_key": chain_key(cfg),
    }


@pytest.mark.parametrize(("fresh_price", "expected_reason"), [
    (340.0, "side_changed_or_pass"),
    (325.0, "quantity_would_overshoot"),
])
def test_stale_dispatch_quote_is_suppressed_before_preview_or_place(
        monkeypatch, fresh_price, expected_reason):
    cfg = main.load_config()
    run_id = "d" * 32
    ck = chain_key(cfg)
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({"committed": True})
    intent = put_intent(ck, run_id, _pending_buy_intent(cfg, run_id))
    monkeypatch.setenv("LEGO_MAX_DISPATCH_PRICE_DRIFT_BPS", "1000")
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_args: [])
    monkeypatch.setattr(main, "fetch_snapshot", lambda *_args: {
        "captured_at": "2026-07-23T18:00:06Z",
        "quote_time": "2026-07-23T18:00:06Z",
        "price": fresh_price,
        "holdings": 9.0,
    })
    monkeypatch.setattr(
        main, "preview_market_order",
        lambda *_args: pytest.fail("stale intent must stop before preview"))
    monkeypatch.setattr(
        main, "place_market_order",
        lambda *_args: pytest.fail("stale intent must never place"))

    result = main._dispatch_or_reconcile_one(object(), object(), cfg, intent)

    assert result["status"] == "SUPPRESSED_STATE_CHANGED"
    assert expected_reason in result["state_change_reasons"]
    stored = FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{run_id}").get()
    assert expected_reason in stored["reasons"]


def test_dispatch_quote_guard_accepts_small_move_that_does_not_overshoot():
    cfg = main.load_config()
    intent = _pending_buy_intent(cfg, "e" * 32)
    verdict = main._dispatch_quote_safety(
        cfg, intent,
        {"captured_at": "2026-07-23T18:00:06Z",
         "quote_time": "2026-07-23T18:00:06Z",
         "price": 319.9, "holdings": 9.0},
        now_utc=SLOT + timedelta(seconds=1),
        max_price_drift_bps=100.0,
        max_quote_age_seconds=360.0)
    assert verdict["ok"] is True
    assert verdict["reasons"] == []


def test_dispatch_quote_guard_rejects_old_decision_even_when_quote_is_fresh():
    cfg = main.load_config()
    intent = _pending_buy_intent(cfg, "f" * 32)
    now = SLOT + timedelta(seconds=361)
    verdict = main._dispatch_quote_safety(
        cfg, intent,
        {"captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "quote_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "price": 320.0, "holdings": 9.0},
        now_utc=now,
        max_price_drift_bps=100.0,
        max_quote_age_seconds=360.0)
    assert verdict["ok"] is False
    assert "decision_age_limit" in verdict["reasons"]
    assert "quote_age_limit" not in verdict["reasons"]


def test_dispatch_quote_guard_rejects_stale_source_quote_with_fresh_decision():
    cfg = main.load_config()
    now = SLOT + timedelta(seconds=1)
    intent = _pending_buy_intent(cfg, "1" * 32)
    verdict = main._dispatch_quote_safety(
        cfg, intent,
        {"captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "quote_time": (now - timedelta(seconds=361)).strftime(
             "%Y-%m-%dT%H:%M:%SZ"),
         "price": 320.0, "holdings": 9.0},
        now_utc=now,
        max_price_drift_bps=100.0,
        max_quote_age_seconds=360.0)
    assert verdict["ok"] is False
    assert "quote_age_limit" in verdict["reasons"]
    assert verdict["quote_age_seconds"] == 361.0


def test_dispatch_quote_guard_rejects_future_or_missing_source_quote():
    cfg = main.load_config()
    now = SLOT + timedelta(seconds=1)
    intent = _pending_buy_intent(cfg, "2" * 32)
    future = main._dispatch_quote_safety(
        cfg, intent,
        {"captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "quote_time": (now + timedelta(
             seconds=main.MAX_DISPATCH_FUTURE_SKEW_SECONDS + 1)).strftime(
                 "%Y-%m-%dT%H:%M:%SZ"),
         "price": 320.0, "holdings": 9.0},
        now_utc=now,
        max_price_drift_bps=100.0,
        max_quote_age_seconds=360.0)
    assert future["ok"] is False
    assert "quote_time_in_future" in future["reasons"]

    with pytest.raises(ValueError, match="quote_time"):
        main._dispatch_quote_safety(
            cfg, intent,
            {"captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "price": 320.0, "holdings": 9.0},
            now_utc=now,
            max_price_drift_bps=100.0,
            max_quote_age_seconds=360.0)


def _committed_pending_buy(cfg, run_id: str) -> tuple[str, dict]:
    ck = chain_key(cfg)
    snapshot = {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }
    row = compute_row(cfg, snapshot, None, dna_step=0)
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({
        **{key: value for key, value in row.items() if key != "_meta"},
        "committed": True,
    })
    return ck, put_intent(ck, run_id, _pending_buy_intent(cfg, run_id))


def test_post_preview_refetch_blocks_changed_quote_before_chain_fence(monkeypatch):
    cfg = main.load_config()
    run_id = "3" * 32
    _ck, intent = _committed_pending_buy(cfg, run_id)
    snapshots = iter([
        {"captured_at": "2026-07-23T18:00:06Z",
         "quote_time": "2026-07-23T18:00:06Z",
         "price": 320.0, "holdings": 9.0},
        {"captured_at": "2026-07-23T18:00:07Z",
         "quote_time": "2026-07-23T18:00:07Z",
         "price": 340.0, "holdings": 9.0},
    ])
    previewed = []
    monkeypatch.setenv("LEGO_MAX_DISPATCH_PRICE_DRIFT_BPS", "1000")
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_args: [])
    monkeypatch.setattr(main, "fetch_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(
        main, "preview_market_order",
        lambda *_args: previewed.append(True) or True)
    monkeypatch.setattr(
        main, "fence_chain_dispatch",
        lambda *_args: pytest.fail("changed post-preview quote must not fence"))
    monkeypatch.setattr(
        main, "place_market_order",
        lambda *_args: pytest.fail("changed post-preview quote must not place"))

    result = main._dispatch_or_reconcile_one(object(), object(), cfg, intent)

    assert previewed == [True]
    assert result["status"] == "SUPPRESSED_STATE_CHANGED"
    assert "side_changed_or_pass" in result["state_change_reasons"]
    stored = FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(cfg)}/{run_id}").get()
    assert stored["dispatch_check_phase"] == "post_preview"


def test_quote_age_is_rechecked_after_slow_chain_fence_before_place(monkeypatch):
    cfg = main.load_config()
    run_id = "4" * 32
    _ck, intent = _committed_pending_buy(cfg, run_id)
    base = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)
    moments = iter([base + timedelta(seconds=1)] * 3
                   + [base + timedelta(seconds=12)])

    class AdvancingNow(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = next(moments)
            return moment.astimezone(tz) if tz else moment

    snapshot = {
        "captured_at": "2026-07-23T18:00:06Z",
        "quote_time": "2026-07-23T18:00:06Z",
        "price": 320.0,
        "holdings": 9.0,
    }
    fenced = []
    monkeypatch.setattr(main, "datetime", AdvancingNow)
    monkeypatch.setenv("LEGO_MAX_DISPATCH_QUOTE_AGE_SECONDS", "10")
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_args: [])
    monkeypatch.setattr(main, "fetch_snapshot", lambda *_args: dict(snapshot))
    monkeypatch.setattr(main, "preview_market_order", lambda *_args: True)
    monkeypatch.setattr(
        main, "fence_chain_dispatch",
        lambda *_args: fenced.append(True) or {"inflight_run_id": run_id})
    monkeypatch.setattr(
        main, "begin_place_attempt",
        lambda *_args: pytest.fail("expired quote must stop before place fence"))
    monkeypatch.setattr(
        main, "place_market_order",
        lambda *_args: pytest.fail("expired quote must never place"))

    result = main._dispatch_or_reconcile_one(
        object(), object(), cfg, intent,
        {"owner": "worker", "claim_token": "token"})

    assert fenced == [True]
    assert result["status"] == "SUPPRESSED_STATE_CHANGED"
    assert "quote_age_limit" in result["state_change_reasons"]
    stored = FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(cfg)}/{run_id}").get()
    assert stored["dispatch_check_phase"] == "pre_place_deadline"


def test_nan_holdings_tolerance_is_config_error_not_fail_open(monkeypatch):
    monkeypatch.setenv("LEGO_HOLDINGS_DRIFT_TOLERANCE", "NaN")
    touched = []
    monkeypatch.setattr(
        main, "build_clients", lambda: touched.append(True) or (object(), object()))

    body, code = main.lego_order_worker(object())

    assert code == 500
    assert body["pipeline_status"] == "CONFIG_ERROR"
    assert touched == []


def test_runtime_identity_is_opaque_alias_stable_and_account_specific(monkeypatch):
    first = webull_io.runtime_identity_fingerprint()
    assert "review-account-a" not in first
    assert len(first) == 64

    monkeypatch.setenv("WEBULL_ENV", "PROD")
    prod = webull_io.runtime_identity_fingerprint()
    monkeypatch.setenv("WEBULL_ENV", "PRODUCTION")
    assert webull_io.runtime_identity_fingerprint() == prod
    assert prod != first

    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "review-account-b")
    assert webull_io.runtime_identity_fingerprint() != first


def test_runtime_identity_mismatch_blocks_existing_chain_without_leaking_id():
    cfg = main.load_config()
    snap = {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }
    row = compute_row(cfg, snap, None, dna_step=0)
    commit_final_row(
        cfg, snap, None, row, runtime_identity="a" * 64)

    with pytest.raises(RuntimeIdentityMismatch) as raised:
        read_anchor(cfg, runtime_identity="b" * 64)
    assert "review-account" not in str(raised.value)


def test_legacy_chain_is_adopted_once_and_says_so(monkeypatch):
    """A pre-guard chain keeps running; the DNA clock never stops to wait."""
    cfg = main.load_config()
    snap0 = {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }
    row0 = compute_row(cfg, snap0, None, dna_step=0)
    commit_final_row(cfg, snap0, None, row0)
    identity = webull_io.runtime_identity_fingerprint()

    stored = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    assert "runtime_identity_fingerprint" not in stored
    assert lego_state.verify_runtime_identity(stored, identity) is True

    anchor = read_anchor(cfg, runtime_identity=identity)
    assert anchor is not None and anchor.version == 1

    body, code = _run_row(monkeypatch, price=321.0)
    assert code == 200 and body["committed"] is True

    stored = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    assert stored["runtime_identity_fingerprint"] == identity
    # Said out loud exactly once, and without the raw account id.
    warned = FAKE_DB.reference(f"{main.WARNINGS_PATH}/runtime_identity_adopted").get()
    assert warned["count"] == 1
    assert identity[:8] in json.dumps(warned) and "review-account" not in json.dumps(warned)

    # Adoption is not a permanent bypass: it is stamped, so the guard now bites.
    assert lego_state.verify_runtime_identity(stored, identity) is False
    with pytest.raises(RuntimeIdentityMismatch):
        read_anchor(cfg, runtime_identity="c" * 64)


def _count_state_reads(monkeypatch, cfg):
    """Count how many times one tick fetches this chain's state document."""
    path = f"{STATE_PATH}/{chain_key(cfg)}"
    reads = []
    real_get = FakeReference.get

    def counting_get(self):
        if "/".join(self.parts) == path:
            reads.append(1)
        return real_get(self)

    monkeypatch.setattr(FakeReference, "get", counting_get)
    return reads


def test_one_tick_reads_the_chain_state_exactly_three_times(monkeypatch):
    """Three reads: one shared by the guards, then the commit's own two.

    The identity guard, the pending-intent recovery and the anchor all need the
    same document, so they share one read. commit_final_row's pre-transaction
    read and the transaction itself must both see a fresh document — that is the
    stale-anchor guard — so they stay. A fourth read means a guard fetched again
    instead of taking the document it was handed.
    """
    cfg = main.load_config()
    reads = _count_state_reads(monkeypatch, cfg)
    body, code = _run_row(monkeypatch)
    assert code == 200 and body["committed"] is True
    assert len(reads) == 3


def test_order_worker_reads_the_chain_state_once(monkeypatch):
    cfg = main.load_config()
    _run_row(monkeypatch)
    reads = _count_state_reads(monkeypatch, cfg)
    assert main._run_order_worker(cfg, limit=1)["processed"] == 0
    assert len(reads) == 1


def test_committed_row_recovers_intent_after_outbox_write_crash(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    real_put = lego_outbox.put_intent

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("fault after state transaction")

    monkeypatch.setattr(main, "put_intent", crash_after_commit)
    body, code = _run_row(monkeypatch)
    assert code == 200 and body["committed"] is True
    assert "fault after state transaction" in body["outbox_error"]

    cfg = main.load_config()
    ck = chain_key(cfg)
    state = FAKE_DB.reference(f"{STATE_PATH}/{ck}").get()
    assert body["run_id"] in state["pending_order_intents"]
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{body['run_id']}").get() is None

    # Simulate the narrower crash: the state transaction and marker committed,
    # but the final row committed=True patch did not. Recovery must repair the
    # row before the outbox becomes visible, or the worker will absorb it as
    # NOT_PLACED.
    row_ref = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}")
    row_ref.update({"committed": False})
    committed_seen_at_put = []

    def assert_row_repaired_before_put(*args, **kwargs):
        committed_seen_at_put.append(row_ref.get().get("committed"))
        return real_put(*args, **kwargs)

    monkeypatch.setattr(main, "put_intent", assert_row_repaired_before_put)
    recovered = main._recover_pending_order_intents(
        cfg, webull_io.runtime_identity_fingerprint())
    assert recovered == 1
    assert committed_seen_at_put == [True]
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{ck}/{body['run_id']}").get()["status"] == "PENDING_DISPATCH"
    assert "pending_order_intents" not in FAKE_DB.reference(
        f"{STATE_PATH}/{ck}").get()


def test_claim_lease_is_atomic_under_real_threads(monkeypatch):
    class AtomicReference:
        def __init__(self):
            self.value = {
                "run_id": "r1",
                "status": "PENDING_DISPATCH",
            }
            self.lock = threading.Lock()

        def transaction(self, fn):
            with self.lock:
                self.value = fn(dict(self.value))
                return dict(self.value)

    ref = AtomicReference()
    monkeypatch.setattr(
        lego_outbox.db, "reference", lambda _path: ref)
    barrier = threading.Barrier(2)

    def compete(worker):
        barrier.wait()
        return claim_intent(
            "ck", "r1", worker, now_utc=SLOT, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("w1", "w2")))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == "PENDING_DISPATCH"
    winner = winners[0]["claim_owner"]
    release_intent_claim("ck", "r1", winner)
    assert "claim_owner" not in ref.value


def test_expired_claim_generation_cannot_cross_the_irreversible_place_fence():
    put_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    first = claim_intent(
        "ck", "r1", "w1", now_utc=SLOT, lease_seconds=1)
    second = claim_intent(
        "ck", "r1", "w2", now_utc=SLOT + timedelta(seconds=2),
        lease_seconds=60)
    assert first is not None and second is not None

    assert begin_place_attempt(
        "ck", "r1", "w1", first["claim_generation"]) is None
    started = begin_place_attempt(
        "ck", "r1", "w2", second["claim_generation"])
    assert started is not None
    assert started["status"] == "PLACING_UNKNOWN"


def test_expired_chain_dispatch_owner_is_replaced_and_cannot_cross_fence():
    first = lego_outbox.claim_chain_dispatch(
        "ck", "worker-1", now_utc=SLOT, lease_seconds=1)
    assert first is not None
    assert lego_outbox.claim_chain_dispatch(
        "ck", "worker-2", now_utc=SLOT, lease_seconds=60) is None

    successor = lego_outbox.claim_chain_dispatch(
        "ck", "worker-2", now_utc=SLOT + timedelta(seconds=2),
        lease_seconds=60)
    assert successor is not None
    assert successor["generation"] == first["generation"] + 1

    # The expired worker cannot renew immediately before the irreversible call.
    assert lego_outbox.fence_chain_dispatch(
        "ck", "r1", "worker-1", first["claim_token"],
        now_utc=SLOT + timedelta(seconds=2), lease_seconds=60) is None
    assert lego_outbox.fence_chain_dispatch(
        "ck", "r2", "worker-2", successor["claim_token"],
        now_utc=SLOT + timedelta(seconds=2), lease_seconds=60) is not None

    # A stale finally block cannot release its successor; the real owner can,
    # and the next worker is then admitted without waiting for lease expiry.
    lego_outbox.release_chain_dispatch("ck", "worker-1", first["claim_token"])
    assert lego_outbox.claim_chain_dispatch(
        "ck", "worker-3", now_utc=SLOT + timedelta(seconds=3),
        lease_seconds=60) is None
    lego_outbox.release_chain_dispatch(
        "ck", "worker-2", successor["claim_token"])
    third = lego_outbox.claim_chain_dispatch(
        "ck", "worker-3", now_utc=SLOT + timedelta(seconds=3),
        lease_seconds=60)
    assert third is not None
    assert third["inflight_run_id"] == "r2"
    # Owner takeover is allowed, but an unresolved broker run survives it and
    # fences every different run_id even after the original lease expired.
    assert lego_outbox.fence_chain_dispatch(
        "ck", "r3", "worker-3", third["claim_token"],
        now_utc=SLOT + timedelta(seconds=3), lease_seconds=60) is None
    assert lego_outbox.clear_chain_dispatch_inflight(
        "ck", "r2", "worker-3", third["claim_token"]) is True
    assert lego_outbox.fence_chain_dispatch(
        "ck", "r3", "worker-3", third["claim_token"],
        now_utc=SLOT + timedelta(seconds=3), lease_seconds=60) is not None


def test_successor_after_hung_place_lease_reconciles_only_the_inflight_run(
        monkeypatch):
    cfg = main.load_config()
    identity = webull_io.runtime_identity_fingerprint()
    ck = chain_key(cfg)
    inflight_run = "a" * 32
    later_run = "b" * 32
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set({
        "version": 1,
        "runtime_identity_fingerprint": identity,
    })
    FAKE_DB.reference(f"webull_lego_rows/{inflight_run}").set({
        "run_id": inflight_run,
        "chain_key": ck,
        "committed": True,
    })
    put_intent(ck, inflight_run, {
        "status": "PLACING_UNKNOWN",
        "place_attempted": True,
        "created_at": "2026-07-23T18:00:00Z",
    })
    put_intent(ck, later_run, {
        "status": "PENDING_DISPATCH",
        "created_at": "2026-07-23T18:30:00Z",
    })

    # The first worker fenced the money call, entered place_order, and hung long
    # enough for only its owner lease to expire. Its run fence must survive.
    old_time = datetime.now(UTC) - timedelta(seconds=10)
    first = lego_outbox.claim_chain_dispatch(
        ck, "hung-worker", now_utc=old_time, lease_seconds=1)
    assert first is not None
    assert lego_outbox.fence_chain_dispatch(
        ck, inflight_run, "hung-worker", first["claim_token"],
        now_utc=old_time, lease_seconds=1) is not None

    details_read = []
    monkeypatch.setattr(main, "fetch_order_detail", lambda _tc, run_id: (
        details_read.append(run_id) or {"order_status": "SUBMITTED"}))
    monkeypatch.setattr(
        main, "place_market_order",
        lambda *_args, **_kwargs: pytest.fail("successor must reconcile, never place"))

    result = main._run_order_worker(cfg, limit=2, runtime_identity=identity)

    assert result["processed"] == 1
    assert result["results"][0]["run_id"] == inflight_run
    assert result["results"][0]["status"] == "SUBMITTED"
    assert details_read == [inflight_run]
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{ck}/{later_run}").get()["status"] == "PENDING_DISPATCH"
    lock = FAKE_DB.reference(f"{lego_outbox.DISPATCH_LOCK_PATH}/{ck}").get()
    assert lock["inflight_run_id"] == inflight_run


@pytest.mark.parametrize("intent", [
    {"status": "RECONCILE_ABANDONED", "needs_manual_check": True},
    {"status": "REALIZED_MATH_ERROR", "needs_manual_check": True,
     "filled_quantity": 1.0},
    {"status": "CASHFLOW_FINALIZE_ERROR", "needs_manual_check": True,
     "filled_quantity": 1.0},
    {"status": "FILLED", "needs_manual_check": True,
     "cashflow_abandoned": True, "filled_quantity": 1.0},
    {"status": "CANCELLED", "filled_quantity": 0.5,
     "cashflow_finalized": False},
])
def test_manual_or_unbooked_terminal_never_releases_chain_money_fence(intent):
    assert main._chain_fence_can_clear(intent) is False


@pytest.mark.parametrize("intent", [
    {"status": "REJECTED", "filled_quantity": 0.0},
    {"status": "FILLED", "filled_quantity": 1.0,
     "filled_price": 100.0, "cashflow_finalized": True, "realized": True,
     "broker_cashflow_recorded": True, "broker_fee_status": "KNOWN"},
    {"status": "EXPIRED", "filled_quantity": 0.5,
     "filled_price": 100.0, "cashflow_finalized": True, "realized": True,
     "broker_cashflow_recorded": True, "broker_fee_status": "KNOWN"},
    {"status": "NOT_PLACED", "place_attempted": False},
])
def test_resolved_terminal_can_release_chain_money_fence(intent):
    assert main._chain_fence_can_clear(intent) is True


@pytest.mark.parametrize("intent", [
    {"status": "CANCELLED"},
    {"status": "CANCELLED", "filled_quantity": "garbage"},
    {"status": "CANCELLED", "filled_quantity": -1},
    {"status": "REJECTED", "filled_quantity": "NaN"},
    {"status": "FILLED", "filled_quantity": 0, "filled_price": 100},
    {"status": "FILLED", "filled_quantity": 1, "filled_price": 100,
     "cashflow_finalized": True, "realized": False},
    {"status": "FILLED", "filled_quantity": 1, "filled_price": None,
     "cashflow_finalized": True, "realized": True},
    {"status": "NOT_PLACED", "place_attempted": True},
    {"status": "SUPPRESSED_STATE_CHANGED", "broker_id": "unexpected"},
])
def test_ambiguous_or_inconsistent_terminal_never_clears_chain_fence(intent):
    assert main._chain_fence_can_clear(intent) is False


def test_archive_keeps_terminal_inflight_intent_until_chain_fence_is_cleared():
    old = "2026-01-01T00:00:00Z"
    cutoff = datetime(2026, 7, 1, tzinfo=UTC)
    put_intent("ck", "r1", {
        "status": "FILLED",
        "filled_quantity": 1.0,
        "cashflow_finalized": True,
        "created_at": old,
        "updated_at": old,
    })
    FAKE_DB.reference(
        f"{lego_outbox.DISPATCH_LOCK_PATH}/ck").set({"inflight_run_id": "r1"})

    assert lego_archive.archive_terminal_intents(cutoff, 10) == 0
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get() is not None
    assert FAKE_DB.reference(
        f"{lego_archive.OUTBOX_ARCHIVE_PATH}/ck/r1").get() is None

    FAKE_DB.reference(f"{lego_outbox.DISPATCH_LOCK_PATH}/ck").delete()
    # Terminal intents remain protected until their new audit marker is repaired.
    main._repair_pending_audits("ck")
    assert lego_archive.archive_terminal_intents(cutoff, 10) == 1
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get() is None
    assert FAKE_DB.reference(
        f"{lego_archive.OUTBOX_ARCHIVE_PATH}/ck/r1").get()["status"] == "FILLED"


def test_two_concurrent_workers_place_exactly_once(monkeypatch):
    cfg = main.load_config()
    identity = webull_io.runtime_identity_fingerprint()
    ck = chain_key(cfg)
    run_id = "a" * 32
    row = compute_row(cfg, {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }, None, dna_step=0)
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set({
        "version": 1,
        "dna_step": 0,
        "p0": 320.0,
        "prev_price": 320.0,
        "prev_actual": 0.0,
        "prev_holdings": 9.0,
        "runtime_identity_fingerprint": identity,
    })
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({
        **{key: value for key, value in row.items() if key != "_meta"},
        "committed": True,
    })
    put_intent(ck, run_id, {
        "status": "PENDING_DISPATCH",
        "row_status": row["สถานะ"],
        "side": row["_meta"]["side"],
        "quantity": row["_meta"]["quantity"],
        "symbol": cfg.symbol,
         "step": row["DNA step"],
         "signal": row["DNA signal"],
         "decision_price": 320.0,
         "decision_holdings": 9.0,
         "decision_time": "2026-07-23T18:00:05Z",
         "created_at": "2026-07-23T18:00:05Z",
        "expires_at": "2099-07-23T18:30:00Z",
    })

    transaction_lock = threading.RLock()
    original_transaction = FakeReference.transaction

    def atomic_transaction(self, fn):
        with transaction_lock:
            return original_transaction(self, fn)

    monkeypatch.setattr(FakeReference, "transaction", atomic_transaction)
    monkeypatch.setattr(main, "fetch_open_orders", lambda _tc, _symbol: [])
    monkeypatch.setattr(main, "fetch_snapshot", lambda _tc, _dc, _cfg: {
        "captured_at": "2026-07-23T18:00:06Z",
        "quote_time": "2026-07-23T18:00:06Z",
        "price": 320.0,
        "holdings": 9.0,
    })
    monkeypatch.setattr(main, "preview_market_order", lambda _tc, _order: True)
    monkeypatch.setattr(main, "fetch_order_detail", lambda _tc, _run_id: {
        "order_status": "FILLED",
        "filled_quantity": row["_meta"]["quantity"],
        "avg_filled_price": 320.0,
        "filled_fee": 0.0,
    })
    monkeypatch.setattr(
        main, "fetch_holdings",
        lambda _tc, _cfg: 9.0 + float(row["_meta"]["quantity"]))
    placed = []
    placed_lock = threading.Lock()

    def place(_tc, _order):
        with placed_lock:
            placed.append(run_id)
        return {"order_status": "FILLED"}

    monkeypatch.setattr(main, "place_market_order", place)
    original_list = main.list_actionable
    barrier = threading.Barrier(2)

    def simultaneous_list(*args, **kwargs):
        docs = original_list(*args, **kwargs)
        barrier.wait(timeout=5)
        return docs

    monkeypatch.setattr(main, "list_actionable", simultaneous_list)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: main._run_order_worker(
                cfg, limit=1, runtime_identity=identity),
            range(2),
        ))

    assert len(placed) == 1
    assert sum(result["processed"] for result in results) == 1
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{ck}/{run_id}").get()["status"] == "FILLED"


def test_two_concurrent_workers_cannot_place_different_intents_on_same_chain(
        monkeypatch):
    """The money fence is per chain, not merely per client_order_id.

    With only per-intent leases, worker A can claim the oldest run while worker B
    skips that claim and wins the next run.  Both then observe no open broker
    order before either irreversible call lands.  The chain fence must serialize
    that window so at most one distinct run_id reaches ``place_order``.
    """
    cfg = main.load_config()
    identity = webull_io.runtime_identity_fingerprint()
    ck = chain_key(cfg)
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set({
        "version": 2,
        "dna_step": 1,
        "p0": 320.0,
        "prev_price": 320.0,
        "prev_actual": 0.0,
        "prev_holdings": 9.0,
        "runtime_identity_fingerprint": identity,
    })

    run_ids = ("a" * 32, "b" * 32)
    for index, run_id in enumerate(run_ids):
        row = compute_row(cfg, {
            "captured_at": f"2026-07-23T18:{index:02d}:05Z",
            "price": 320.0,
            "holdings": 9.0,
        }, None, dna_step=index)
        FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({
            **{key: value for key, value in row.items() if key != "_meta"},
            "committed": True,
        })
        put_intent(ck, run_id, {
            "status": "PENDING_DISPATCH",
            "row_status": row["สถานะ"],
            "side": row["_meta"]["side"],
            "quantity": row["_meta"]["quantity"],
             "symbol": cfg.symbol,
             "step": row["DNA step"],
             "signal": row["DNA signal"],
             "decision_price": 320.0,
             "decision_holdings": 9.0,
             "decision_time": f"2026-07-23T18:{index:02d}:05Z",
             "created_at": f"2026-07-23T18:{index:02d}:05Z",
            "slot_start_utc": f"2026-07-23T18:{index:02d}:00Z",
            "expires_at": "2099-07-23T18:30:00Z",
        })

    transaction_lock = threading.RLock()
    original_transaction = FakeReference.transaction

    def atomic_transaction(self, fn):
        with transaction_lock:
            return original_transaction(self, fn)

    monkeypatch.setattr(FakeReference, "transaction", atomic_transaction)
    monkeypatch.setattr(main, "fetch_snapshot", lambda _tc, _dc, _cfg: {
        "captured_at": "2026-07-23T18:02:00Z",
        "quote_time": "2026-07-23T18:02:00Z",
        "price": 320.0,
        "holdings": 9.0,
    })
    monkeypatch.setattr(main, "preview_market_order", lambda _tc, _order: True)
    monkeypatch.setattr(
        main, "fetch_order_detail",
        lambda _tc, _run_id: {"order_status": "SUBMITTED"})
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    # If both workers reach this broker read, they both see the same pre-place
    # world.  A per-chain lease prevents the second worker from reaching it.
    broker_read_barrier = threading.Barrier(2)
    placed: list[str] = []
    placed_lock = threading.Lock()

    def no_open_orders(_tc, _symbol):
        with placed_lock:
            if placed:
                return [{"client_order_id": placed[0]}]
        try:
            broker_read_barrier.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            # Expected after the fix: only the chain-lease owner reaches here.
            pass
        return []

    monkeypatch.setattr(main, "fetch_open_orders", no_open_orders)

    def place(_tc, order):
        with placed_lock:
            placed.append(order[0]["client_order_id"])
        return {"order_status": "SUBMITTED"}

    monkeypatch.setattr(main, "place_market_order", place)
    original_list = main.list_actionable
    list_barrier = threading.Barrier(2)

    def simultaneous_list(*args, **kwargs):
        docs = original_list(*args, **kwargs)
        list_barrier.wait(timeout=5)
        return docs

    monkeypatch.setattr(main, "list_actionable", simultaneous_list)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: main._run_order_worker(
                cfg, limit=2, runtime_identity=identity),
            range(2),
        ))

    assert len(placed) == 1, placed
    assert sum(result["processed"] for result in results) == 1


def test_audit_failure_is_repaired_from_authoritative_outbox(monkeypatch):
    put_intent("ck", "r1", {
        "status": "PENDING_DISPATCH",
        "created_at": "2026-07-23T18:00:00Z",
    })
    real_update = lego_state.update_order_audit

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(main, "update_order_audit", fail_audit)
    main._persist("ck", "r1", {
        "status": "PLACING_UNKNOWN",
        "place_attempted": True,
    })
    intent = FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get()
    assert intent["status"] == "PLACING_UNKNOWN"
    assert intent["audit_pending"] is True

    monkeypatch.setattr(main, "update_order_audit", real_update)
    assert main._repair_pending_audits("ck") == 1
    intent = FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get()
    audit = FAKE_DB.reference("webull_lego_order_audit/r1").get()
    assert intent["audit_pending"] is False
    assert audit["status"] == "PLACING_UNKNOWN"


def test_persisted_and_returned_errors_redact_credentials(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_SECRET", "top-secret-value")
    put_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    result = main._persist_error(
        "ck", "r1", "NOT_PLACED",
        RuntimeError(
            "account_id=review-account-a app_secret=top-secret-value"),
    )
    serialized = json.dumps({
        "result": result,
        "outbox": FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get(),
        "audit": FAKE_DB.reference("webull_lego_order_audit/r1").get(),
    })
    assert "review-account-a" not in serialized
    assert "top-secret-value" not in serialized
    assert "<redacted>" in serialized


def test_incomplete_pagination_keeps_intent_pending_and_never_places(monkeypatch):
    cfg = main.load_config()
    run_id = "b" * 32
    ck = chain_key(cfg)
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({"committed": True})
    intent = put_intent(ck, run_id, {
        "status": "PENDING_DISPATCH",
        "expires_at": "2099-07-23T18:30:00Z",
    })
    monkeypatch.setattr(
        main, "fetch_open_orders",
        lambda *_args: (_ for _ in ()).throw(
            webull_io.IncompleteOpenOrdersError("truncated")),
    )
    placed = []
    monkeypatch.setattr(
        main, "place_market_order", lambda *_args: placed.append(True))

    result = main._dispatch_or_reconcile_one(object(), object(), cfg, intent)

    assert result["status"] == "PENDING_DISPATCH"
    assert placed == []
    stored = FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{run_id}").get()
    assert stored["status"] == "PENDING_DISPATCH"
    assert stored["pagination_complete"] is False


def test_missing_fill_fields_become_terminal_manual_check():
    intent = put_intent("ck", "r1", {
        "status": "PLACING_UNKNOWN",
        "side": "BUY",
    })
    result = main._finish_with_realized(object(), main.load_config(), intent, {
        "status": "FILLED",
        "realized": True,
    })
    assert result["status"] == "REALIZED_MATH_ERROR"
    assert result["needs_manual_check"] is True
    assert result["broker_status"] == "FILLED"


def test_fee_only_update_and_replay_are_counted_exactly_once():
    buy = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.0)
    assert buy["realized_delta"] == 0.0
    replay = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.0)
    assert replay["realized_delta"] == 0.0

    late_fee = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.25)
    assert late_fee["realized_delta"] == pytest.approx(-0.25)
    assert late_fee["realized_cumulative"] == pytest.approx(-0.25)
    replay_fee = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.25)
    assert replay_fee["realized_delta"] == 0.0
    assert replay_fee["realized_cumulative"] == pytest.approx(-0.25)
    with pytest.raises(ValueError, match="average fill price"):
        apply_realized_fill("ck", "buy", "BUY", 1.0, 101.0, 0.25)

    close = apply_realized_fill("ck", "sell", "SELL", 1.0, 110.0, 0.10)
    assert close["realized_cumulative"] == pytest.approx(9.65)
    stored = FAKE_DB.reference(f"{REALIZED_PATH}/ck").get()
    assert stored["applied_fills"]["buy"]["fee"] == pytest.approx(0.25)
    assert stored["applied_fills"]["buy"]["realized_delta"] == pytest.approx(-0.25)
    assert stored["applied_fills"]["buy"]["seq"] == 2
    assert stored["applied_fills"]["sell"]["seq"] == 3
    assert stored["applied_fills"]["sell"]["cumulative_realized_after"] \
        == pytest.approx(9.65)
    assert stored["applied_seq"] == 3
    assert stored["last_event_id"] == "sell"
    assert stored["applied_fills"]["sell"]["open_legs_after_hash"] \
        == lego_state.realized_open_legs_hash(stored["open_legs"])


@pytest.mark.parametrize("args", [
    ("BUY", float("nan"), 100.0, 0.0),
    ("BUY", 1.0, float("inf"), 0.0),
    ("BUY", 1.0, 100.0, float("nan")),
    ("HOLD", 1.0, 100.0, 0.0),
])
def test_realized_ledger_rejects_nonfinite_or_unknown_fill_evidence(args):
    with pytest.raises(ValueError):
        apply_realized_fill("ck", "bad", *args)
    assert FAKE_DB.reference(f"{REALIZED_PATH}/ck").get() is None


def test_late_fee_refuses_corrupt_fifo_legs_instead_of_witnessing_them():
    apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.0)
    FAKE_DB.reference(f"{REALIZED_PATH}/ck/open_legs").set({
        "buys": [[1.0, 100.0, 0.0]],
        "sells": [[1.0, 101.0, 0.0]],
    })
    with pytest.raises(ValueError, match="buy/sell open legs"):
        apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.1)


def test_terminal_outbox_status_cannot_be_reopened():
    put_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    update_intent("ck", "r1", {"status": "FILLED"})
    update_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get()["status"] == "FILLED"
