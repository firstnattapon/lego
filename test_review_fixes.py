"""Regressions for the review findings: silence, wrong blame, and unbounded paths.

Every test here failed before the corresponding fix. The theme is that the
pipeline used to answer 'fine' — or answer the wrong question — in situations
where a human needed to be told something specific.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from conftest import FAKE_DB
from lego_archive import (AUDIT_ARCHIVE_PATH, OUTBOX_ARCHIVE_PATH,
                          archive_terminal_records)
from lego_outbox import OUTBOX_PATH, list_actionable
from lego_state import AUDIT_PATH, REALIZED_PATH, chain_key

UTC = timezone.utc
SESSION_OPEN_SLOT = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)     # ordinal 0 on a 30m grid
NEXT_SLOT = datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC)


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
    for key in ("AUTO_SUBMIT", "LEGO_INLINE_ORDER_WORKER", "LEGO_DNA_LOW_WATERMARK",
                "LEGO_MARKET_HOLIDAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    # A healthy token by default: the tests below own the warnings node and
    # a real token file does not exist in a test process.
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})


@pytest.fixture
def auto_submit(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    monkeypatch.setenv("WEBULL_ENV", "UAT")


def _run(monkeypatch, moment: datetime, price: float, holdings: float = 0.0):
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def _stub_broker(monkeypatch, *, place=None, detail=None, preview=True,
                 holdings_after=None):
    monkeypatch.setattr(main, "preview_market_order", lambda tc, o: preview)
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "place_market_order",
                        place or (lambda tc, o: {"order_status": "FILLED"}))
    detail_fn = detail or (lambda tc, r: {})

    def broker_detail(tc, run_id):
        payload = dict(detail_fn(tc, run_id))
        if (payload.get("filled_quantity") is not None
                and "filled_fee" not in payload):
            payload["filled_fee"] = 0.0
        return payload

    monkeypatch.setattr(main, "fetch_order_detail", broker_detail)
    # The post-execution position read. Default 0.0 means "the position never
    # moved", which is what these order-path tests need: no fill is confirmed,
    # so none of them accidentally finalizes a model cashflow.
    monkeypatch.setattr(main, "fetch_holdings",
                        lambda tc, cfg: 0.0 if holdings_after is None
                        else float(holdings_after))


# --- F1: a degraded clock must not skip the order in silence ----------------

def test_degraded_clock_says_it_sent_no_order(monkeypatch, auto_submit):
    """Deploying the README ENVS example lands here: no origin, so no slot, so
    no intent — and the response used to read exactly like a healthy one."""
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "shadow")
    monkeypatch.delenv("LEGO_DNA_ORIGIN_UTC")
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)

    assert code == 200
    assert body["status"] == "READY_BUY" and body["committed"] is True
    assert body["clock_mode"] == "shadow:degraded"
    assert "ไม่มีการสร้าง order intent" in body["outbox_skipped"]
    assert list_actionable(chain_key(main.load_config())) == []

    warning = FAKE_DB.reference("webull_lego_warnings/degraded_clock_no_order").get()
    assert warning["count"] == 1 and warning["row_status"] == "READY_BUY"
    assert "LEGO_DNA_ORIGIN_UTC" in warning["hint"]


def test_degraded_warning_counts_instead_of_piling_up(monkeypatch, auto_submit):
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "shadow")
    monkeypatch.delenv("LEGO_DNA_ORIGIN_UTC")
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    _run(monkeypatch, NEXT_SLOT, 321.0)
    warnings = FAKE_DB.reference("webull_lego_warnings").get()
    assert list(warnings) == ["degraded_clock_no_order"]
    assert warnings["degraded_clock_no_order"]["count"] == 2


def test_healthy_clock_still_creates_the_intent_quietly(monkeypatch, auto_submit):
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert "outbox_skipped" not in body
    assert [i["run_id"] for i in list_actionable(chain_key(main.load_config()))] == [body["run_id"]]
    assert FAKE_DB.reference("webull_lego_warnings").get() is None


def test_a_pass_row_needs_no_skip_notice(monkeypatch, auto_submit):
    """Nothing was going to be ordered anyway, so the degraded clock costs the
    row nothing and must not raise a warning."""
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "shadow")
    monkeypatch.delenv("LEGO_DNA_ORIGIN_UTC")
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 300.0, holdings=10.0)   # gap = 0
    assert code == 200 and body["status"] == "PASS_THRESHOLD"
    assert "outbox_skipped" not in body
    assert FAKE_DB.reference("webull_lego_warnings").get() is None


# --- F3: DNA running out is an end state, not an outage ---------------------

def test_exhausted_dna_answers_200_with_its_own_status(monkeypatch):
    monkeypatch.setenv("LEGO_DNA_CODE", "bypass:1")
    first, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200 and first["committed"] is True

    body, code = _run(monkeypatch, NEXT_SLOT, 321.0)
    assert code == 200
    assert body["pipeline_status"] == "DNA_EXHAUSTED"
    assert body["status"] == "PASS_DNA_EXHAUSTED" and body["committed"] is False
    assert "step=1 len=1" in body["note"]
    assert FAKE_DB.reference("webull_lego_errors").get() is None   # not an error


def test_dna_warns_before_it_runs_out(monkeypatch):
    monkeypatch.setenv("LEGO_DNA_CODE", "bypass:3")
    monkeypatch.setenv("LEGO_DNA_LOW_WATERMARK", "2")
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert body["dna_steps_remaining"] == 2


def test_a_healthy_chain_keeps_its_usual_response(monkeypatch):
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)      # bypass:100, step 0
    assert "dna_steps_remaining" not in body


# --- F4: a fill we cannot price is not an unresolved order ------------------

def _seed_realized(ck: str, run_id: str) -> None:
    """A prior cumulative fill that makes the next tail fill price impossible."""
    FAKE_DB.reference(f"{REALIZED_PATH}/{ck}").set({
        "applied_fills": {run_id: {"quantity": 1.0, "fee": 0.0,
                                   "average_price": 1000.0, "side": "BUY"}},
        "open_legs": {"buys": [[1.0, 1000.0, 0.0]], "sells": []},
        "cumulative_realized": 0.0,
    })


def test_impossible_realized_math_does_not_read_as_a_lost_order(monkeypatch, auto_submit):
    cfg = main.load_config()
    ck = chain_key(cfg)
    _stub_broker(monkeypatch, detail=lambda tc, r: {
        "order_status": "FILLED",
        "filled_quantity": FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{r}").get()["quantity"],
        "avg_filled_price": 50.0})
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    _seed_realized(ck, body["run_id"])

    result = main._run_order_worker(cfg, limit=1)["results"][0]
    assert result["status"] == "REALIZED_MATH_ERROR"

    intent = FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{body['run_id']}").get()
    assert intent["needs_manual_check"] is True
    assert "reconcile_attempts" not in intent          # retrying cannot fix arithmetic
    assert intent["broker_status"] == "FILLED"         # the fill is not in doubt
    assert float(intent["filled_quantity"]) == intent["quantity"]
    assert intent["filled_price"] == "50.0"

    audit = FAKE_DB.reference(f"{AUDIT_PATH}/{body['run_id']}").get()
    assert audit["needs_manual_check"] is True and audit["realized"] is False
    assert "realized" in audit["last_error"] or "price" in audit["last_error"]
    assert FAKE_DB.reference("webull_lego_warnings/realized_math_error").get()["count"] == 1


def test_a_ledger_gap_leaves_the_dispatch_queue(monkeypatch, auto_submit):
    """It must not sit actionable forever — that is the starvation the reconcile
    budget was introduced to prevent, arriving by another door."""
    cfg = main.load_config()
    ck = chain_key(cfg)
    _stub_broker(monkeypatch, detail=lambda tc, r: {
        "order_status": "FILLED",
        "filled_quantity": FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{r}").get()["quantity"],
        "avg_filled_price": 50.0})
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    _seed_realized(ck, body["run_id"])
    main._run_order_worker(cfg, limit=1)

    assert list_actionable(ck) == []
    placed = []
    _stub_broker(monkeypatch, place=lambda tc, o: placed.append(o) or {"order_status": "FILLED"},
                 detail=lambda tc, r: {"order_status": "FILLED"})
    main._run_order_worker(cfg, limit=1)
    assert placed == []                                # and never re-sent


def test_broker_silence_still_spends_the_reconcile_budget(monkeypatch, auto_submit):
    """The split must not weaken the other branch."""
    monkeypatch.setenv("LEGO_RECONCILE_MAX_ATTEMPTS", "2")
    cfg = main.load_config()

    def unreachable(*args, **kwargs):
        raise RuntimeError("broker unreachable")

    _stub_broker(monkeypatch, place=unreachable)
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    seen = [main._run_order_worker(cfg, limit=1)["results"][0]["status"] for _ in range(2)]
    assert seen == ["PLACING_UNKNOWN", "RECONCILE_ABANDONED"]


# --- F5: the confirmation gate now compares two different sources ----------

def test_an_intent_that_drifts_from_the_committed_row_is_not_sent(monkeypatch, auto_submit):
    cfg = main.load_config()
    ck = chain_key(cfg)
    placed = []
    _stub_broker(monkeypatch, place=lambda tc, o: placed.append(o) or {"order_status": "FILLED"},
                 detail=lambda tc, r: {"order_status": "FILLED"})
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)

    # Whatever the cause — a bad write, a hand edit, a future bug — the outbox no
    # longer says what the engine decided.
    FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{body['run_id']}").update({"quantity": 999.0})
    result = main._run_order_worker(cfg, limit=1)["results"][0]

    assert result["status"] == "NOT_PLACED"
    assert "confirmation phrase" in result["error"]
    assert placed == []


def test_a_matching_intent_still_passes_the_gate(monkeypatch, auto_submit):
    cfg = main.load_config()
    placed = []
    _stub_broker(monkeypatch, place=lambda tc, o: placed.append(o) or {"order_status": "FILLED"},
                 detail=lambda tc, r: {
                     "order_status": "FILLED",
                     "filled_quantity": 1.0,
                     "avg_filled_price": 320.0,
                 },
                 holdings_after=1.0)          # the position actually moved
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    result = main._run_order_worker(cfg, limit=1)["results"][0]
    assert result["status"] == "FILLED" and len(placed) == 1
    # ...and the fill, not the decision, is what booked the model ledger.
    assert result["cashflow_finalized"] is True
    row = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert row["cashflow_status"] == "FINALIZED"
    assert row["execution_price"] == 320.0 and row["post_execution_holdings"] == 1.0


def test_a_row_flipped_to_pass_after_commit_blocks_the_order(monkeypatch, auto_submit):
    cfg = main.load_config()
    placed = []
    _stub_broker(monkeypatch, place=lambda tc, o: placed.append(o) or {"order_status": "FILLED"})
    body, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").update({"สถานะ": "PASS_THRESHOLD"})

    result = main._run_order_worker(cfg, limit=1)["results"][0]
    assert result["status"] == "NOT_PLACED" and placed == []


# --- F7: finished records leave the paths the live loops scan ---------------

def _outbox_doc(status: str, updated_at: str | None, **extra) -> dict:
    doc = {"status": status, "run_id": "x", **extra}
    if updated_at:
        doc["updated_at"] = updated_at
        if status == "PENDING_DISPATCH":
            doc["actionable_sort"] = updated_at
    return doc


def test_archive_moves_only_finished_and_dated_records():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    FAKE_DB.reference(f"{OUTBOX_PATH}/ck1").set({
        "finished_old": _outbox_doc("FILLED", old),
        "finished_recent": _outbox_doc("FILLED", recent),
        "still_open": _outbox_doc("PENDING_DISPATCH", old),
        "awaiting_human": _outbox_doc("RECONCILE_ABANDONED", old, needs_manual_check=True),
        "undated": _outbox_doc("FILLED", None),
    })

    moved = archive_terminal_records(now, days=30, limit=500)
    assert moved["intents_archived"] == 1
    assert sorted(FAKE_DB.reference(f"{OUTBOX_PATH}/ck1").get()) == [
        "awaiting_human", "finished_recent", "still_open", "undated"]
    assert FAKE_DB.reference(f"{OUTBOX_ARCHIVE_PATH}/ck1/finished_old").get()["status"] == "FILLED"
    # The dispatchable view is untouched: only finished work left the path.
    assert [i["status"] for i in list_actionable("ck1")] == ["PENDING_DISPATCH"]


def test_archive_moves_finished_audits_and_keeps_the_open_ones():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    FAKE_DB.reference(AUDIT_PATH).set({
        "a": {"status": "FILLED", "placed_at": old},
        "b": {"status": "REJECTED", "placed_at": old},
        "c": {"status": "PLACING_UNKNOWN", "placed_at": old},
        "d": {"status": "FILLED", "placed_at": old, "needs_manual_check": True},
    })
    moved = archive_terminal_records(now, days=30, limit=500)
    assert moved["audits_archived"] == 2
    assert sorted(FAKE_DB.reference(AUDIT_PATH).get()) == ["c", "d"]
    assert sorted(FAKE_DB.reference(AUDIT_ARCHIVE_PATH).get()) == ["a", "b"]


def test_archive_respects_its_batch_limit():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    FAKE_DB.reference(f"{OUTBOX_PATH}/ck1").set(
        {f"r{i}": _outbox_doc("FILLED", old) for i in range(5)})
    assert archive_terminal_records(now, days=30, limit=2)["intents_archived"] == 2
    assert len(FAKE_DB.reference(f"{OUTBOX_PATH}/ck1").get()) == 3


def test_archive_does_not_delete_a_concurrent_recovery_update(monkeypatch):
    """Eligibility becoming false after copy must retain the live witness."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_path = f"{OUTBOX_PATH}/ck1/race"
    FAKE_DB.reference(source_path).set(_outbox_doc("FILLED", old))

    real_reference = FAKE_DB.reference
    source_transactions = 0

    def reference_with_race(path=""):
        ref = real_reference(path)
        if path != source_path:
            return ref
        real_transaction = ref.transaction

        def transaction(fn):
            nonlocal source_transactions
            source_transactions += 1
            if source_transactions == 2:
                current = ref.get()
                current.update({"status": "NEEDS_MANUAL_CHECK",
                                "needs_manual_check": True,
                                "recovery_note": "operator evidence arrived"})
                ref.set(current)
            return real_transaction(fn)

        ref.transaction = transaction
        return ref

    monkeypatch.setattr("lego_archive.db.reference", reference_with_race)
    moved = archive_terminal_records(now, days=30, limit=500)

    assert moved["intents_archived"] == 0
    assert FAKE_DB.reference(source_path).get()["recovery_note"] == \
        "operator evidence arrived"
    assert FAKE_DB.reference(f"{OUTBOX_ARCHIVE_PATH}/ck1/race").get() is None


def test_archive_worker_reports_what_it_moved(monkeypatch):
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(main, "datetime", _fixed_now(now))
    FAKE_DB.reference(f"{OUTBOX_PATH}/ck1").set({"r": _outbox_doc("FILLED", old)})

    body, code = main.lego_archive_worker(object())
    assert code == 200 and body["pipeline_status"] == "ARCHIVE_OK"
    assert body["intents_archived"] == 1 and body["retention_days"] == 30


def test_archive_worker_never_takes_the_scheduler_down(monkeypatch):
    monkeypatch.setattr(main, "archive_terminal_records",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rtdb down")))
    body, code = main.lego_archive_worker(object())
    assert code == 503 and "rtdb down" in body["error"]
