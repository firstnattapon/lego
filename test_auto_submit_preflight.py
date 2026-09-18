"""The AUTO_SUBMIT checklist, as tests.

Every condition the deploy checklist used to ask a human to verify is exercised
here twice: once as a unit against the pure evaluator, and once end-to-end
through lego_one_row, where the only acceptable outcome of a failed check is
"row committed, no order intent, and the reason said out loud".

The regression half of the file pins what must NOT move: the 17-column contract,
the recurrence numbers (against three real production rows), and the response
shape of a healthy slot.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

import main
from conftest import FAKE_DB
from lego_one_row import COLUMN_ORDER, ExecutionFill, columns_presented
from lego_orders import PROD, UAT
from lego_outbox import OUTBOX_PATH, list_actionable
from lego_preflight import (CHECK_IDS, DEGRADED_CLOCK_MESSAGE,
                            auto_submit_preflight,
                            evaluate_auto_submit_preflight)
from lego_state import STATE_PATH, chain_key, finalize_execution_fill

UTC = timezone.utc
SLOT_0 = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)      # ordinal 0 on a 30m grid
SLOT_1 = datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC)
SLOT_2 = datetime(2026, 7, 23, 19, 0, 5, tzinfo=UTC)

# The three rows this chain actually committed in production on 2026-07-27.
PROD_HOLDINGS = 9.14492
PROD_PRICES = (335.55, 335.15, 336.71)


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
        "WEBULL_ENV": "UAT",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("AUTO_SUBMIT", "LEGO_INLINE_ORDER_WORKER", "LEGO_DNA_LOW_WATERMARK",
                "LEGO_MARKET_HOLIDAYS", "LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "token_health", lambda: {"ok": True, "reasons": []})


@pytest.fixture
def auto_submit(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "true")


def _run(monkeypatch, moment: datetime, price: float, holdings: float = 9.0):
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def _intents():
    return list_actionable(chain_key(main.load_config()))


def _warnings():
    return FAKE_DB.reference("webull_lego_warnings").get()


class _Slot:
    def __init__(self, ordinal):
        self.market_ordinal = ordinal


def _report(**overrides):
    kwargs = {
        "auto_submit": True, "environment": UAT,
        "row": {"สถานะ": "READY_BUY", "DNA step": 4, "_meta": {"quantity": 0.2}},
        "row_durable": True, "slot": _Slot(4),
        "token": {"ok": True, "reasons": []}, "dna_remaining": 95,
    }
    kwargs.update(overrides)
    return evaluate_auto_submit_preflight(**kwargs)


# --- unit: the evaluator itself ---------------------------------------------

def test_every_declared_check_is_evaluated():
    """Checklist Coverage: the report answers all of CHECK_IDS, not a subset."""
    assert [c["id"] for c in _report()["checks"]] == list(CHECK_IDS)


def test_a_ready_deployment_passes():
    report = _report()
    assert report["ok"] is True
    assert report["blocked_by"] == [] and report["message"] == ""


@pytest.mark.parametrize("check_id, override", [
    ("auto_submit_enabled", {"auto_submit": False}),
    ("environment_uat", {"environment": PROD}),
    ("row_durable", {"row_durable": False}),
    ("row_actionable", {"row": {"สถานะ": "PASS_THRESHOLD", "DNA step": 4,
                                "_meta": {"quantity": 0.0}}}),
    ("clock_not_degraded", {"slot": None}),
    ("step_matches_market_ordinal", {"slot": _Slot(7)}),
    ("token_ready", {"token": {"ok": False, "reasons": ["token dir /tmp"]}}),
    ("dna_headroom", {"dna_remaining": 0}),
])
def test_each_check_blocks_on_its_own(check_id, override):
    report = _report(**override)
    assert report["ok"] is False
    assert check_id in report["blocked_by"]


def test_unreadable_row_fails_closed():
    """Case 5 — data partially missing: no quantity means no order."""
    for row in ({"สถานะ": "READY_BUY", "DNA step": 4}, {}, None,
                {"สถานะ": "READY_BUY", "DNA step": None, "_meta": {"quantity": "x"}}):
        report = _report(row=row)
        assert report["ok"] is False
        assert "row_actionable" in report["blocked_by"]


def test_token_reasons_reach_the_message():
    report = _report(token={"ok": False, "reasons": ["token dir /tmp/webull_token"]})
    assert "/tmp/webull_token" in report["message"]


def test_missing_token_report_is_not_a_pass():
    for token in (None, {}, "ok", {"ok": "true"}):
        assert _report(token=token)["ok"] is False


def test_degraded_clock_keeps_its_legacy_channel():
    report = _report(slot=None)
    assert report["field"] == "outbox_skipped"
    assert report["warning_kind"] == "degraded_clock_no_order"
    assert report["message"] == DEGRADED_CLOCK_MESSAGE
    assert "LEGO_DNA_ORIGIN_UTC" in report["hint"]
    # The ordinal check cannot run without a slot and must not be counted as a
    # pass; it is excluded as not applicable while the clock check holds the gate.
    ordinal_check = [c for c in report["checks"]
                     if c["id"] == "step_matches_market_ordinal"][0]
    assert ordinal_check["applicable"] is False and ordinal_check["ok"] is False
    assert report["blocked_by"] == ["clock_not_degraded"]


def test_several_failures_are_all_reported():
    """Case 4 — more than one unmet condition."""
    report = _report(token={"ok": False, "reasons": ["ephemeral"]}, dna_remaining=0,
                     row_durable=False)
    assert report["blocked_by"] == ["row_durable", "token_ready", "dna_headroom"]
    assert len(report["reasons"]) == 3
    assert report["message"] == report["reasons"][0]        # first failure leads


def test_min_dna_remaining_is_configurable():
    assert _report(dna_remaining=5, min_dna_remaining=10)["ok"] is False
    assert _report(dna_remaining=5, min_dna_remaining=5)["ok"] is True


def test_unreadable_dna_remaining_fails_closed():
    assert _report(dna_remaining=None)["ok"] is False
    assert _report(dna_remaining="many")["ok"] is False


def test_wrapper_turns_an_exception_into_a_block():
    """Case 8 — the checklist itself fails."""
    report = auto_submit_preflight(auto_submit=True)        # missing kwargs
    assert report["ok"] is False
    assert report["blocked_by"] == ["preflight_error"]
    assert report["field"] == "outbox_blocked"


def test_wrapper_blocks_when_the_evaluator_raises(monkeypatch):
    import lego_preflight

    def boom(**kwargs):
        raise RuntimeError("checklist exploded")
    monkeypatch.setattr(lego_preflight, "evaluate_auto_submit_preflight", boom)
    report = lego_preflight.auto_submit_preflight()
    assert report["ok"] is False and "checklist exploded" in report["message"]


# --- end-to-end: lego_one_row -----------------------------------------------

def test_auto_submit_off_creates_nothing(monkeypatch):
    """Case 1 — the default state stays exactly as quiet as it was."""
    body, code = _run(monkeypatch, SLOT_0, 320.0)
    assert code == 200 and body["status"] == "READY_BUY"
    assert _intents() == [] and _warnings() is None
    assert "outbox_blocked" not in body and "outbox_skipped" not in body


def test_ready_deployment_still_creates_the_intent(monkeypatch, auto_submit):
    """Case 2 — a passing checklist must not cost the order."""
    body, code = _run(monkeypatch, SLOT_0, 320.0)
    assert code == 200 and body["committed"] is True
    assert [i["run_id"] for i in _intents()] == [body["run_id"]]
    assert "outbox_blocked" not in body and _warnings() is None


def test_ephemeral_token_blocks_the_order(monkeypatch, auto_submit):
    """Case 3 — one unmet condition, the one the production log is showing."""
    monkeypatch.setattr(main, "token_health", lambda: {
        "ok": False,
        "reasons": ["token dir /tmp/webull_token อยู่บน storage ที่หายเมื่อ instance ถูกรีไซเคิล"]})
    body, code = _run(monkeypatch, SLOT_0, 320.0)

    assert code == 200
    assert body["committed"] is True                    # DNA time never stops
    assert body["status"] == "READY_BUY"
    assert _intents() == []
    assert "/tmp/webull_token" in body["outbox_blocked"]
    assert body["outbox_blocked_checks"] == ["token_ready"]
    warning = FAKE_DB.reference("webull_lego_warnings/auto_submit_blocked").get()
    assert warning["count"] == 1 and warning["row_status"] == "READY_BUY"
    assert warning["blocked_by"] == ["token_ready"]


def test_several_unmet_conditions_are_all_named(monkeypatch, auto_submit):
    """Case 4, end to end: exhausted DNA and a dying token at once."""
    monkeypatch.setenv("LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING", "500")
    monkeypatch.setattr(main, "token_health", lambda: {"ok": False, "reasons": ["dying"]})
    body, code = _run(monkeypatch, SLOT_0, 320.0)
    assert code == 200 and _intents() == []
    assert body["outbox_blocked_checks"] == ["token_ready", "dna_headroom"]


def test_missing_origin_still_answers_the_old_way(monkeypatch, auto_submit):
    """Case 6 — incomplete config. The degraded clock keeps its exact wording,
    its own warning kind and its hint, because a dashboard reads them."""
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "shadow")
    monkeypatch.delenv("LEGO_DNA_ORIGIN_UTC")
    body, code = _run(monkeypatch, SLOT_0, 320.0)

    assert code == 200 and body["clock_mode"] == "shadow:degraded"
    assert body["outbox_skipped"] == DEGRADED_CLOCK_MESSAGE
    assert "outbox_blocked" not in body
    assert _intents() == []
    warning = FAKE_DB.reference("webull_lego_warnings/degraded_clock_no_order").get()
    assert warning["count"] == 1 and warning["row_status"] == "READY_BUY"
    assert "LEGO_DNA_ORIGIN_UTC" in warning["hint"]


def test_shadow_step_that_is_not_the_market_ordinal_is_blocked(monkeypatch, auto_submit):
    """Case 7 — state mismatch, and the fail-open this gate was written for.

    Shadow mode counts anchor + 1, so a missed slot leaves the row's step behind
    the market ordinal. Committing that is fine; sending a real order on it is
    trading a slot the DNA was never trained on.
    """
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "shadow")
    aligned, _ = _run(monkeypatch, SLOT_0, 320.0)          # step 0 = ordinal 0, sendable
    body, code = _run(monkeypatch, SLOT_2, 322.0)          # ordinal 2, shadow step 1

    assert code == 200 and body["committed"] is True
    assert (body["step"], body["market_step"]) == (1, 2)
    assert body["outbox_blocked_checks"] == ["step_matches_market_ordinal"]
    assert [i["run_id"] for i in _intents()] == [aligned["run_id"]]   # no second intent
    assert FAKE_DB.reference("webull_lego_warnings/auto_submit_blocked").get()["count"] == 1


def test_market_mode_is_aligned_by_construction(monkeypatch, auto_submit):
    """The same missed slot in market mode jumps the step and stays sendable."""
    _run(monkeypatch, SLOT_0, 320.0)
    body, _ = _run(monkeypatch, SLOT_2, 322.0)
    assert (body["step"], body["market_step"]) == (2, 2)
    assert "outbox_blocked" not in body
    assert len(_intents()) == 2


def test_a_failed_intent_write_is_still_only_the_orders_problem(monkeypatch, auto_submit):
    """Case 9 — exception during submission, after the checklist passed."""
    def boom(*args, **kwargs):
        raise RuntimeError("RTDB write failed")
    monkeypatch.setattr(main, "put_intent", boom)

    body, code = _run(monkeypatch, SLOT_0, 320.0)
    assert code == 200 and body["committed"] is True
    assert "RTDB write failed" in body["outbox_error"]
    assert "outbox_blocked" not in body                    # the gate did pass
    assert FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()["committed"] is True


def test_preflight_failure_never_reaches_the_outbox(monkeypatch, auto_submit):
    """Case 8 end to end: a checklist that raises blocks the order, and only the
    order — the DNA row it was asked about is still committed."""
    import lego_preflight

    def boom(**kwargs):
        raise RuntimeError("checklist exploded")
    monkeypatch.setattr(lego_preflight, "evaluate_auto_submit_preflight", boom)

    body, code = _run(monkeypatch, SLOT_0, 320.0)
    assert code == 200 and body["committed"] is True
    assert _intents() == []
    assert "checklist exploded" in body["outbox_blocked"]
    assert body["outbox_blocked_checks"] == ["preflight_error"]


def test_an_intent_is_never_reopened_by_a_second_put(monkeypatch, auto_submit):
    """Case 10 — duplicate submission. The client_order_id is the run_id, which is
    derived from the snapshot rather than generated, and put_intent is a
    create-if-absent transaction: an intent already in flight cannot be reset to
    PENDING_DISPATCH and sent a second time."""
    first, _ = _run(monkeypatch, SLOT_0, 320.0)
    ck = chain_key(main.load_config())
    assert [i["client_order_id"] for i in _intents()] == [first["run_id"]]

    main.update_intent(ck, first["run_id"],
                       {"status": "PLACING_UNKNOWN", "place_attempted": True})
    main.put_intent(ck, first["run_id"], {"status": "PENDING_DISPATCH", "quantity": 99})

    outbox = FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}").get()
    assert list(outbox) == [first["run_id"]]
    assert outbox[first["run_id"]]["status"] == "PLACING_UNKNOWN"   # not reopened
    assert outbox[first["run_id"]]["quantity"] != 99                # not overwritten


def test_run_id_is_derived_not_generated(monkeypatch):
    """The same snapshot must always name the same order, on every retry."""
    from lego_state import make_run_id
    snapshot = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    ck = chain_key(main.load_config())
    assert make_run_id(ck, 0, snapshot) == make_run_id(ck, 0, snapshot)
    assert make_run_id(ck, 0, snapshot) != make_run_id(ck, 1, snapshot)


def test_concurrent_ticks_in_one_slot_produce_one_intent(monkeypatch, auto_submit):
    """Case 11 — two invocations racing inside the same slot. The slot guard
    stops the second before any intent exists."""
    first, _ = _run(monkeypatch, SLOT_0, 320.0)
    second, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 20, 0, tzinfo=UTC), 321.0)

    assert code == 200 and second["pipeline_status"] == "SLOT_CONSUMED"
    assert second["committed"] is False
    assert [i["run_id"] for i in _intents()] == [first["run_id"]]
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}").get()
    assert state["version"] == 1


# --- regression: UI, calculation, output ------------------------------------

def test_column_contract_is_untouched(monkeypatch, auto_submit):
    """Case 12 — UI. The 17 columns, their order, and the presented rounding."""
    assert len(COLUMN_ORDER) == 18
    assert COLUMN_ORDER[0] == "เวลา (UTC)" and COLUMN_ORDER[-1] == "Eₙ ส่วนเกินสะสม (USD)"

    body, _ = _run(monkeypatch, SLOT_0, 335.55, holdings=PROD_HOLDINGS)
    doc = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert [k for k in doc if k in COLUMN_ORDER] == COLUMN_ORDER
    assert set(doc) - set(COLUMN_ORDER) == {
            "run_id", "chain_key", "version", "committed", "semantics",
            "market_slot_id", "market_ordinal", "clock_mode", "cashflow_status",
            "instrument_capability"}
    assert columns_presented({k: doc[k] for k in COLUMN_ORDER})["ราคา Pₙ (USD)"] == 335.55


def test_recurrence_matches_the_production_rows(monkeypatch, auto_submit):
    """Case 13 — calculation. Three real slots, replayed: Rₙ, ΔAₙ, Aₙ, Eₙ and the
    quantity must still land on the numbers the live chain committed.

    The arithmetic is unchanged; what moved is *when* it runs. lego_one_row now
    commits each row with the cashflow carried forward, and the same numbers
    appear once each order is confirmed filled at its decision price — which is
    what this replay simulates, one finalize per committed slot."""
    fix_c, p0 = 3000.0, PROD_PRICES[0]
    rows = []
    for moment, price in zip((SLOT_0, SLOT_1, SLOT_2), PROD_PRICES):
        body, code = _run(monkeypatch, moment, price, holdings=PROD_HOLDINGS)
        assert code == 200 and body["status"] == "READY_SELL"
        committed = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
        # Committed on decision alone: no fill yet, so no cashflow yet.
        assert committed["cashflow_status"] == "PENDING_EXECUTION"
        assert committed["ΔAₙ ต่อสเต็ป (USD)"] == 0.0
        # Then the broker fills it at that price and the position moves.
        quantity = committed["จำนวนสั่ง (หุ้น)"]
        finalize_execution_fill(
            main.load_config(), body["run_id"],
            ExecutionFill(filled_price=price, filled_quantity=quantity,
                          holdings_after=PROD_HOLDINGS - quantity))
        rows.append(FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get())

    assert [r["cashflow_status"] for r in rows] == ["FINALIZED"] * 3
    presented = [columns_presented({k: r[k] for k in COLUMN_ORDER}) for r in rows]
    assert [p["มูลค่าพอร์ต (USD)"] for p in presented] == [3068.58, 3064.92, 3079.19]
    assert [p["ส่วนต่างเป้าหมาย (USD)"] for p in presented] == [68.58, 64.92, 79.19]
    assert [p["จำนวนสั่ง (หุ้น)"] for p in presented] == [0.2, 0.19, 0.24]
    assert [p["Rₙ อ้างอิง (USD)"] for p in presented] == [0, -3.58, 10.35]
    assert [p["ΔAₙ ต่อสเต็ป (USD)"] for p in presented] == [0, -3.58, 13.96]
    assert [p["Aₙ สะสม (USD)"] for p in presented] == [0, -3.58, 10.39]
    assert [p["Eₙ ส่วนเกินสะสม (USD)"] for p in presented] == [0, 0, 0.03]

    # ...and against the equations themselves, not just the stored numbers.
    for i, price in enumerate(PROD_PRICES[1:], start=1):
        assert rows[i]["Rₙ อ้างอิง (USD)"] == pytest.approx(fix_c * math.log(price / p0))
        assert rows[i]["ΔAₙ ต่อสเต็ป (USD)"] == pytest.approx(
            fix_c * (price / PROD_PRICES[i - 1] - 1.0))
        assert rows[i]["Eₙ ส่วนเกินสะสม (USD)"] >= -1e-9        # Eₙ = Aₙ − Rₙ ≥ 0


def test_blocked_rows_keep_the_same_ledger(monkeypatch, auto_submit):
    """The gate touches orders only: a blocked slot commits the same numbers."""
    monkeypatch.setattr(main, "token_health", lambda: {"ok": False, "reasons": ["dying"]})
    body, _ = _run(monkeypatch, SLOT_0, 335.55, holdings=PROD_HOLDINGS)
    doc = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert doc["สถานะ"] == "READY_SELL" and doc["committed"] is True
    assert round(doc["ส่วนต่างเป้าหมาย (USD)"], 2) == 68.58
    assert round(doc["จำนวนสั่ง (หุ้น)"], 2) == 0.2
    assert doc["market_ordinal"] == 0 and doc["clock_mode"] == "market"


def test_healthy_response_shape_is_unchanged(monkeypatch, auto_submit):
    """Case 14 — output. A passing slot answers with exactly the keys it always
    did, plus cashflow_semantics; the rest appear only when something is blocked.

    cashflow_semantics is unconditional on purpose. Identifying which revision
    produced a row previously meant reading Cloud Run, and a stale revision that
    still books a PASS row as an act answers 200 with a healthy-looking body —
    the exact shape this test asserts. Naming the accounting in that body is what
    makes runtime and repository comparable from the caller's side.
    """
    body, code = _run(monkeypatch, SLOT_0, 320.0)
    assert code == 200
    assert set(body) == {
        "status", "committed", "idempotent", "run_id", "version", "step", "signal",
        "model_acted", "pipeline_status", "clock_mode", "legacy_step", "market_step",
        "alignment_error", "market_slot_id", "cashflow_semantics"}
    assert body["cashflow_semantics"] == "execution_confirmed_v1"


def test_low_watermark_notice_survived_the_refactor(monkeypatch, auto_submit):
    monkeypatch.setenv("LEGO_DNA_CODE", "bypass:3")
    monkeypatch.setenv("LEGO_DNA_LOW_WATERMARK", "10")
    body, _ = _run(monkeypatch, SLOT_0, 320.0)
    assert body["dna_steps_remaining"] == 2


# --- the /tmp deadlock, end to end ------------------------------------------

def test_an_accepted_ephemeral_dir_lets_the_intent_through(monkeypatch, auto_submit):
    """The production stall of 2026-07-27, fixed.

    Ten consecutive READY_SELL rows committed and the broker position never
    moved, because token_health's `ok` folded 'the token dir is /tmp' — true on
    every Cloud Functions deployment — into the same boolean the order gate
    read. The gate now reads `ready`, so an accepted durability risk stops
    costing the order while the warning still reaches the response.
    """
    monkeypatch.setattr(main, "token_health", lambda: {
        "ok": False, "ready": True,
        "reasons": ["token dir /tmp/webull_token อยู่บน storage ที่หายเมื่อ instance ถูกรีไซเคิล"]})
    body, code = _run(monkeypatch, SLOT_0, 335.55, holdings=PROD_HOLDINGS)

    assert code == 200 and body["committed"] is True
    assert body["status"] == "READY_SELL"
    assert [i["run_id"] for i in _intents()] == [body["run_id"]]
    assert "outbox_blocked" not in body
    # Accepted, not silenced: the operator still sees the risk every slot.
    assert "/tmp/webull_token" in body["token_warning"]
    assert FAKE_DB.reference("webull_lego_warnings/webull_token").get() is not None


def test_a_health_report_without_ready_keeps_the_strict_reading(monkeypatch, auto_submit):
    """Case 3 unchanged: `ok` is still the verdict when there is no `ready`."""
    monkeypatch.setattr(main, "token_health", lambda: {
        "ok": False, "reasons": ["token dir /tmp/webull_token"]})
    body, _ = _run(monkeypatch, SLOT_0, 320.0)
    assert _intents() == [] and body["outbox_blocked_checks"] == ["token_ready"]


def test_ready_false_blocks_whatever_ok_says(monkeypatch, auto_submit):
    """`ready` is the gate, so a report that lies about `ok` cannot open it."""
    monkeypatch.setattr(main, "token_health", lambda: {
        "ok": True, "ready": False, "reasons": ["ไม่พบ token file"]})
    body, _ = _run(monkeypatch, SLOT_0, 320.0)
    assert _intents() == [] and body["outbox_blocked_checks"] == ["token_ready"]
