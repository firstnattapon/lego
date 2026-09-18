"""Why a committed READY_SELL never reached Webull, and what now makes it.

The production chain on 2026-07-28 committed nine consecutive READY_SELL rows
against AAPL and sent zero orders. Every column read healthy, every HTTP call
answered 200, and the position never moved. The block was `token_ready`: on Cloud
Functions the only writable path is /tmp, `token_dir_is_ephemeral()` is therefore
True on every stock deployment, and with `LEGO_ALLOW_EPHEMERAL_TOKEN_DIR` unset
that closed `token_health()["ready"]` — so `auto_submit_preflight` refused a token
that had just signed two authenticated calls in the same invocation.

The tests here pin both halves of the answer:

* `durability_risk_only` separates "this token cannot sign a request" from "this
  token's directory will not survive a recycle", and `live_proof_supersedable`
  adds the one other finding a local file inspection cannot settle — no token
  file at all, on a broker app that never asks for one. Live proof of a signed
  request satisfies those two and nothing else; a rejected or expiring token is
  the broker's own verdict and still blocks.
  (The missing-file half is the 2026-07-29/30 outage; see
  test_holdings_never_moved_incident.py, which replays that export.)
* the delivery path itself, from intent through preview, place, order detail and
  the position read back afterwards, including the acceptance equations for
  delivery rate, fill confirmation and duplicate orders.

Numbers are the production ones wherever a fixture allows it, so a regression
reproduces the exported row rather than an invented one.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

import main
import webull_io
from conftest import FAKE_DB
from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN,
                          REFERENCE_COLUMN, Anchor, Config, compute_row,
                          dna_steps_remaining)
from lego_orders import UAT
from lego_outbox import OUTBOX_PATH, list_actionable
from lego_preflight import evaluate_auto_submit_preflight
from lego_state import CASHFLOW_FINALIZED, CASHFLOW_PENDING, chain_key
from market_clock import resolve_market_slot
from webull_io import build_order_payload, quantity_string

UTC = timezone.utc

# ---- the production run, from the Cloud Run revision and the row export ------
# env of the deployed revision: AUTO_SUBMIT=true, LEGO_DECIMAL_PRECISION=3,
# LEGO_DIFF=5, LEGO_FIX_C=3000, LEGO_SLOT_SECONDS=900, LEGO_DNA_CLOCK_MODE=market,
# LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z, LEGO_SYMBOL=AAPL, WEBULL_ENV=UAT.
PROD_ENV = {
    "LEGO_SYMBOL": "AAPL",
    "LEGO_FIX_C": "3000",
    "LEGO_DIFF": "5",
    "LEGO_DNA_CODE": "bypass:100",
    "LEGO_DECIMAL_PRECISION": "3",
    "LEGO_SLOT_SECONDS": "900",
    "LEGO_DNA_ORIGIN_UTC": "2026-07-27T13:30:00Z",
    "LEGO_DNA_CLOCK_MODE": "market",
    "FIREBASE_DB_URL": "https://x.firebaseio.com",
    "AUTO_SUBMIT": "true",
    "WEBULL_ENV": "UAT",
}
# Row 0 of the export: 2026-07-28T17:50:04Z, slot 2026-07-28:17, ordinal 43.
PROD_MOMENT = datetime(2026, 7, 28, 17, 50, 4, tzinfo=UTC)
PROD_SLOT_ID = "2026-07-28:17"
PROD_ORDINAL = 43
PROD_PRICE = 339.15
PROD_HOLDINGS = 9.14492
PROD_QUANTITY = 0.299
FIX_C = 3000.0


def _fixed_now(moment: datetime):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return _Now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    for key, value in PROD_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("LEGO_INLINE_ORDER_WORKER", "LEGO_DNA_LOW_WATERMARK",
                "LEGO_MARKET_HOLIDAYS", "LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING",
                "LEGO_ALLOW_EPHEMERAL_TOKEN_DIR", "WEBULL_TOKEN_DIR",
                "LEGO_FILL_CONFIRM_MAX_ATTEMPTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "ORDER_POLL_DELAY_S", 0.0)


def _write_token(tmp_path, monkeypatch, *, days_left=14.0, status="NORMAL"):
    """A real token file in a real ephemeral dir — no token_health double.

    The outage lived in token_health's own arithmetic, so these tests must run it
    for real. pytest's tmp_path sits under the platform temp dir, which is what
    `token_dir_is_ephemeral()` recognizes; that is asserted rather than assumed so
    a platform where it does not hold fails loudly instead of passing vacuously.
    """
    token_dir = tmp_path / "webull_token"
    token_dir.mkdir(parents=True, exist_ok=True)
    expires_ms = int((datetime.now(UTC) + timedelta(days=days_left)).timestamp() * 1000)
    (token_dir / "token.txt").write_text(f"tok-abc\n{expires_ms}\n{status}\n",
                                         encoding="utf-8")
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(token_dir))
    assert webull_io.token_dir_is_ephemeral() is True
    return token_dir


def _run(monkeypatch, moment=PROD_MOMENT, price=PROD_PRICE, holdings=PROD_HOLDINGS):
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def _cfg():
    return main.load_config()


def _intents():
    return list_actionable(chain_key(_cfg()))


def _intent(run_id):
    return FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(_cfg())}/{run_id}").get()


def _row(run_id):
    return FAKE_DB.reference(f"webull_lego_rows/{run_id}").get()


def _report(**overrides):
    kwargs = {
        "auto_submit": True, "environment": UAT,
        "row": {"สถานะ": "READY_SELL", "DNA step": 43, "_meta": {"quantity": 0.299}},
        "row_durable": True, "slot": _Slot(43),
        "token": {"ok": True, "ready": True, "durability_risk_only": False,
                  "reasons": []},
        "dna_remaining": 56,
    }
    kwargs.update(overrides)
    return evaluate_auto_submit_preflight(**kwargs)


class _Slot:
    def __init__(self, ordinal):
        self.market_ordinal = ordinal


# --- A. token_health: the three states, not two ------------------------------

def test_an_ephemeral_dir_with_a_good_token_is_durability_risk_only(
        tmp_path, monkeypatch):
    """The production shape. Blocked, but only about the *next* container."""
    _write_token(tmp_path, monkeypatch)
    health = webull_io.token_health()

    assert health["found"] is True and health["status"] == "NORMAL"
    assert health["ok"] is False and health["ready"] is False
    assert health["durability_risk_only"] is True
    assert len(health["reasons"]) == 1
    assert "storage ที่หายเมื่อ instance ถูกรีไซเคิล" in health["reasons"][0]


def test_a_missing_token_is_supersedable_but_never_durability_risk_only(
        tmp_path, monkeypatch):
    """Two different questions, and no token file answers them differently.

    It is not a durability risk — that word is reserved for the *next* container
    — so `durability_risk_only` stays False and the narrow exemption still
    refuses it. It is a local-file finding, though, and a broker app with token
    checking disabled never produces the file at all, so live proof of a signed
    request supersedes it.
    """
    token_dir = _write_token(tmp_path, monkeypatch)
    (token_dir / "token.txt").unlink()

    health = webull_io.token_health()
    assert health["ready"] is False
    assert health["durability_risk_only"] is False
    assert health["live_proof_supersedable"] is True


@pytest.mark.parametrize("days_left,status", [(1.0, "NORMAL"), (14.0, "EXPIRED")])
def test_a_dying_or_rejected_token_is_never_durability_risk_only(
        tmp_path, monkeypatch, days_left, status):
    _write_token(tmp_path, monkeypatch, days_left=days_left, status=status)

    health = webull_io.token_health()
    assert health["ready"] is False
    assert health["durability_risk_only"] is False
    assert len(health["reasons"]) == 2          # the dir, and the token itself


def test_a_durable_dir_has_no_risk_to_classify(tmp_path, monkeypatch):
    """Nothing wrong at all is not 'durability only' — there is no reason to be."""
    token_dir = tmp_path / "durable"
    token_dir.mkdir()
    expires_ms = int((datetime.now(UTC) + timedelta(days=14)).timestamp() * 1000)
    (token_dir / "token.txt").write_text(f"tok\n{expires_ms}\nNORMAL\n",
                                         encoding="utf-8")
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(token_dir))
    monkeypatch.setattr(webull_io, "token_dir_is_ephemeral", lambda: False)

    health = webull_io.token_health()
    assert health["ok"] is True and health["ready"] is True
    assert health["reasons"] == [] and health["durability_risk_only"] is False


def test_accepting_the_ephemeral_dir_still_reports_the_risk(tmp_path, monkeypatch):
    """The operator flag moves `ready`, not the classification or the warning."""
    _write_token(tmp_path, monkeypatch)
    monkeypatch.setenv("LEGO_ALLOW_EPHEMERAL_TOKEN_DIR", "true")

    health = webull_io.token_health()
    assert health["ready"] is True and health["ok"] is False
    assert health["durability_risk_only"] is True


# --- B. preflight: live proof answers exactly one question -------------------

def test_live_proof_clears_a_durability_only_block():
    token = {"ok": False, "ready": False, "durability_risk_only": True,
             "reasons": ["token dir /tmp/webull_token …"]}
    assert _report(token=token)["blocked_by"] == ["token_ready"]
    assert _report(token=token, token_proved_live=True)["ok"] is True


def test_live_proof_does_not_excuse_a_token_the_broker_judged():
    """The narrowness is the safety: a verdict about the token still blocks.

    A rejected or expiring token is the broker's answer, not something a local
    inspection invented, so neither flag may be set for it and live proof changes
    nothing.
    """
    for reasons in (["token status=EXPIRED"],
                    ["token เหลืออีก 1.00 วันก่อนหมดอายุ"]):
        token = {"ok": False, "ready": False, "durability_risk_only": False,
                 "live_proof_supersedable": False, "reasons": reasons}
        report = _report(token=token, token_proved_live=True)
        assert report["ok"] is False
        assert report["blocked_by"] == ["token_ready"]


def test_live_proof_clears_a_token_file_the_broker_never_asked_for():
    """The wider exemption, and it is still opt-in on both sides."""
    token = {"ok": False, "ready": False, "durability_risk_only": False,
             "live_proof_supersedable": True,
             "reasons": ["ไม่พบ token file ที่ /tmp/webull_token/token.txt …"]}
    assert _report(token=token)["blocked_by"] == ["token_ready"]
    assert _report(token=token, token_proved_live=False)["blocked_by"] == [
        "token_ready"]
    assert _report(token=token, token_proved_live=True)["ok"] is True
    # A health report from before this field existed keeps the strict reading.
    assert _report(token={"ok": False, "ready": False,
                          "durability_risk_only": False,
                          "reasons": ["ไม่พบ token file"]},
                   token_proved_live=True)["blocked_by"] == ["token_ready"]


def test_live_proof_is_opt_in_so_older_callers_keep_the_strict_reading():
    token = {"ok": False, "ready": False, "durability_risk_only": True,
             "reasons": ["ephemeral"]}
    assert _report(token=token)["blocked_by"] == ["token_ready"]         # default
    assert _report(token=token, token_proved_live=False)["blocked_by"] == [
        "token_ready"]
    # A health report from before this field existed cannot claim the exemption.
    assert _report(token={"ok": False, "reasons": ["ephemeral"]},
                   token_proved_live=True)["blocked_by"] == ["token_ready"]


def test_live_proof_forgives_nothing_but_the_token_check():
    """It is evidence about a token, so it says nothing about any other check."""
    token = {"ok": False, "ready": False, "durability_risk_only": True,
             "reasons": ["ephemeral"]}
    for override, blocked in (
            ({"auto_submit": False}, "auto_submit_enabled"),
            ({"row_durable": False}, "row_durable"),
            ({"slot": _Slot(99)}, "step_matches_market_ordinal"),
            ({"dna_remaining": 0}, "dna_headroom"),
    ):
        report = _report(token=token, token_proved_live=True, **override)
        assert report["ok"] is False and blocked in report["blocked_by"]


# --- C. the production slot, end to end, with token_health running for real ---

def test_the_production_slot_now_creates_an_intent(tmp_path, monkeypatch):
    """The regression this whole change exists for.

    Same env, same moment, same price and position as export row 0. Before the
    fix: committed READY_SELL, outbox_blocked=['token_ready'], zero intents.
    """
    _write_token(tmp_path, monkeypatch)
    body, code = _run(monkeypatch)

    assert code == 200 and body["committed"] is True
    assert body["status"] == "READY_SELL"
    assert body["market_slot_id"] == PROD_SLOT_ID
    assert body["step"] == body["market_step"] == PROD_ORDINAL
    # legacy_step is anchor+1 and this is the genesis row, so shadow time
    # reads 0 while market time reads 43. Recorded, never acted on.
    assert body["alignment_error"] == -PROD_ORDINAL

    # The order now exists, and the durability risk is still reported out loud.
    assert "outbox_blocked" not in body and "outbox_blocked_checks" not in body
    assert "storage ที่หายเมื่อ instance ถูกรีไซเคิล" in body["token_warning"]
    warning = FAKE_DB.reference("webull_lego_warnings/webull_token").get()
    assert warning["count"] == 1 and warning["ephemeral_token_dir"] is True
    assert FAKE_DB.reference(
        "webull_lego_warnings/auto_submit_blocked").get() is None

    intent = _intent(body["run_id"])
    assert intent["status"] == "PENDING_DISPATCH"
    assert intent["side"] == "SELL"
    assert intent["quantity"] == PROD_QUANTITY
    assert intent["client_order_id"] == body["run_id"]
    assert intent["decision_holdings"] == PROD_HOLDINGS
    assert intent["slot_id"] == PROD_SLOT_ID
    # expires_at = slot_end - 15s margin, so the order cannot cross into slot 18.
    assert intent["expires_at"] == "2026-07-28T17:59:45Z"


def test_the_production_row_is_bit_identical_to_the_export(monkeypatch):
    """The decision half was never wrong, and must not move now."""
    cfg = _cfg()
    monkeypatch.setattr(main, "datetime", _fixed_now(PROD_MOMENT))
    slot = resolve_market_slot(PROD_MOMENT)
    assert (slot.slot_id, slot.market_ordinal) == (PROD_SLOT_ID, PROD_ORDINAL)

    row = compute_row(
        cfg,
        {"captured_at": "2026-07-28T17:50:04Z", "price": PROD_PRICE,
         "holdings": PROD_HOLDINGS},
        Anchor(version=1, dna_step=42, p0=PROD_PRICE, prev_price=PROD_PRICE,
               prev_actual=0.0, prev_holdings=PROD_HOLDINGS),
        dna_step=slot.market_ordinal)

    assert row["สถานะ"] == "READY_SELL" and row["ฝั่ง"] == "SELL"
    assert row["จำนวนสั่ง (หุ้น)"] == PROD_QUANTITY
    assert round(row["มูลค่าพอร์ต (USD)"], 2) == 3101.5
    assert round(row["ส่วนต่างเป้าหมาย (USD)"], 2) == 101.5
    # READY_* is an intent: the three cashflow columns stay carried forward.
    assert row[DELTA_COLUMN] == 0.0 and row[ACTUAL_COLUMN] == 0.0


def test_a_token_file_the_broker_never_asked_for_does_not_block(
        tmp_path, monkeypatch):
    """The second outage, same column: 2026-07-29/30, AAPL, 8.78392 shares flat.

    On the UAT app the chain runs under, ClientInitializer logs
    `_check_token_enable result is False` on every call and returns before
    TokenManager exists, so token.txt is never written. token_health read that as
    'nothing to sign with' and shut the gate on every slot: twenty-two consecutive
    committed READY_BUY/READY_SELL rows, zero intents, and `จำนวนถือครอง (หุ้น)`
    frozen at 8.78392 from the first row to the last.

    The snapshot in `_run` is the live proof — the same authenticated client that
    would place the order just read the account position — so the missing file is
    no longer the last word. The durability warning is still emitted, and the row
    itself is untouched.
    """
    token_dir = _write_token(tmp_path, monkeypatch)
    (token_dir / "token.txt").unlink()

    body, code = _run(monkeypatch)

    assert code == 200 and body["committed"] is True      # DNA time never stops
    assert body["status"] == "READY_SELL"
    assert "outbox_blocked" not in body and "outbox_blocked_checks" not in body
    assert FAKE_DB.reference(
        "webull_lego_warnings/auto_submit_blocked").get() is None
    # Still said out loud: the operator needs to know there is no token file.
    assert "ไม่พบ token file" in body["token_warning"]

    intent = _intent(body["run_id"])
    assert intent["status"] == "PENDING_DISPATCH"
    assert intent["side"] == "SELL" and intent["quantity"] == PROD_QUANTITY


@pytest.mark.parametrize("days_left,status", [(1.0, "NORMAL"), (14.0, "EXPIRED")])
def test_a_dying_or_rejected_token_still_blocks_the_production_slot(
        tmp_path, monkeypatch, days_left, status):
    """The gate is still a gate for every verdict about the token itself.

    Live proof widened by exactly one local-file finding. A token the broker
    rejected, or one about to expire, is not a file-inspection artefact — it is
    an answer about this token — so a call that happened to succeed a moment ago
    does not buy it a pass.
    """
    _write_token(tmp_path, monkeypatch, days_left=days_left, status=status)

    body, code = _run(monkeypatch)

    assert code == 200 and body["committed"] is True      # DNA time never stops
    assert _intents() == []
    assert body["outbox_blocked_checks"] == ["token_ready"]
    warning = FAKE_DB.reference("webull_lego_warnings/auto_submit_blocked").get()
    assert warning["blocked_by"] == ["token_ready"]
    assert warning["row_status"] == "READY_SELL"


def test_the_live_proof_is_never_claimed_without_the_call_that_earns_it(
        tmp_path, monkeypatch):
    """No snapshot, no proof, no row, no intent — the exemption cannot lead."""
    _write_token(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "datetime", _fixed_now(PROD_MOMENT))

    def _boom(_t, _d, _cfg):
        raise RuntimeError("positions unavailable")

    monkeypatch.setattr(main, "fetch_snapshot", _boom)
    body, code = main.lego_one_row(object())

    assert code == 500 and body["pipeline_status"] == "SNAPSHOT_OR_ENGINE_ERROR"
    assert _intents() == []
    assert FAKE_DB.reference("webull_lego_rows").get() is None


def test_auto_submit_off_creates_no_intent_even_with_a_ready_token(
        tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTO_SUBMIT", "false")

    body, code = _run(monkeypatch)
    assert code == 200 and body["status"] == "READY_SELL"
    assert _intents() == [] and "outbox_blocked" not in body


def test_a_pass_row_creates_no_intent(tmp_path, monkeypatch):
    """holdings × price inside the ±DIFF band: nothing to send."""
    _write_token(tmp_path, monkeypatch)
    # 8.846 × 339.15 = 2999.98 → |gap| < DIFF = 5
    body, code = _run(monkeypatch, holdings=8.846)

    assert code == 200 and body["status"] == "PASS_THRESHOLD"
    assert _intents() == [] and "outbox_blocked" not in body


def test_a_ready_buy_creates_a_buy_intent(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch, holdings=8.0)

    assert body["status"] == "READY_BUY"
    intent = _intent(body["run_id"])
    assert intent["side"] == "BUY"
    assert intent["quantity"] == round((FIX_C - 8.0 * PROD_PRICE) / PROD_PRICE, 3)


# --- D. the worker: an idle tick has to be legible ---------------------------

def test_an_idle_worker_says_so_and_spends_no_auth_call(monkeypatch):
    """40 idle ticks answered 200 in 0.3s with nothing to read.

    An empty outbox and a broken pipeline looked identical from Cloud Logging,
    which is why the diagnosis needed a CSV export. It also authenticated four
    times per tick — two config plus two create_token, against a cap of ten per
    thirty seconds shared with lego_one_row — to do no work.
    """
    built = []
    monkeypatch.setattr(main, "build_clients",
                        lambda: built.append(True) or (object(), object()))

    out = main._run_order_worker(_cfg(), limit=3)

    assert out == {"processed": 0, "actionable": 0, "expired_unsent": 0,
                   "results": []}
    assert built == []


def test_a_busy_worker_reports_what_it_had_to_do(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    _stub_broker(monkeypatch, detail={"order_status": "SUBMITTED"},
                 holdings_after=PROD_HOLDINGS)

    out = main._run_order_worker(_cfg(), limit=3)
    assert out["actionable"] == 1 and out["processed"] == 1
    assert out["results"][0]["run_id"] == body["run_id"]


def _stub_broker(monkeypatch, *, detail, holdings_after, place=None, preview=True):
    monkeypatch.setattr(main, "preview_market_order",
                        preview if callable(preview) else (lambda tc, o: preview))
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "place_market_order",
                        place or (lambda tc, o: {"order_status": "SUBMITTED"}))
    detail_fn = detail if callable(detail) else (lambda tc, r: detail)

    def broker_detail(tc, run_id):
        payload = dict(detail_fn(tc, run_id))
        if (payload.get("filled_quantity") is not None
                and "filled_fee" not in payload):
            payload["filled_fee"] = 0.0
        return payload

    monkeypatch.setattr(main, "fetch_order_detail", broker_detail)
    monkeypatch.setattr(main, "fetch_holdings",
                        holdings_after if callable(holdings_after)
                        else (lambda tc, cfg: float(holdings_after)))


# --- E. delivery, end to end, against the acceptance equations ---------------

def test_the_full_uat_path_from_intent_to_holdings_after(tmp_path, monkeypatch):
    """One order, all the way: preview → place → detail → position → ledger.

    Checks the acceptance equations the specification asks for:
      order_delivery_rate   = orders_reaching_place_order / eligible_ready = 1
      sell_confirmation     : holdings_after <= holdings_before - filled + tol
      duplicate_order_rate  = duplicate place_order calls / intents      = 0
    """
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    run_id = body["run_id"]
    holdings_before = PROD_HOLDINGS

    previewed, placed, details = [], [], []
    fill_price = 339.20
    filled_qty = PROD_QUANTITY
    holdings_after = holdings_before - filled_qty

    def _preview(_tc, order):
        previewed.append(order)
        return True

    def _place(_tc, order):
        placed.append(order)
        return {"order_status": "SUBMITTED"}

    def _detail(_tc, client_order_id):
        details.append(client_order_id)
        return {"order_status": "FILLED", "filled_quantity": filled_qty,
                "avg_filled_price": fill_price, "transaction_fee": 0.0}

    _stub_broker(monkeypatch, detail=_detail, holdings_after=holdings_after,
                 place=_place, preview=_preview)

    result = main._run_order_worker(_cfg(), limit=3)["results"][0]

    # 1. the order reached the broker, exactly once, under the deterministic id
    assert len(placed) == 1                                    # delivery rate 1/1
    assert placed[0] == previewed[0]                            # preview == placed
    assert placed[0][0]["client_order_id"] == run_id
    assert details and set(details) == {run_id}

    # 2. the payload is the Manual Test Lab payload, field for field
    #    (Webull_Dashboard/manual_tools.py::build_market_order_payload)
    assert placed[0] == [{
        "combo_type": "NORMAL",
        "client_order_id": run_id,
        "symbol": "AAPL",
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": "0.299",
        "side": "SELL",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "support_trading_session": "CORE",
    }]

    # 3. the fill is confirmed by two independent witnesses
    assert result["status"] == "FILLED"
    assert result["cashflow_finalized"] is True
    assert result["post_execution_holdings"] == pytest.approx(holdings_after)
    tolerance = 1e-6
    assert holdings_after <= holdings_before - filled_qty + tolerance

    # 4. the model ledger moved on the filled price, not the decision price
    row = _row(run_id)
    expected_dA = FIX_C * (fill_price / PROD_PRICE - 1.0)
    assert row["cashflow_status"] == CASHFLOW_FINALIZED
    assert row[DELTA_COLUMN] == pytest.approx(expected_dA)
    assert row[ACTUAL_COLUMN] == pytest.approx(expected_dA)
    assert row[EXCESS_COLUMN] == pytest.approx(expected_dA - row[REFERENCE_COLUMN])
    assert row["execution_price"] == fill_price
    # Rₙ is a genesis row against its own price: ln(P/P) = 0.
    assert row[REFERENCE_COLUMN] == pytest.approx(0.0)

    # 5. duplicate_order_rate = 0: the intent is terminal and re-running is a no-op
    assert _intent(run_id)["status"] == "FILLED"
    assert _intents() == []
    main._run_order_worker(_cfg(), limit=3)
    assert len(placed) == 1


def test_a_place_that_times_out_is_never_sent_twice(tmp_path, monkeypatch):
    """A timeout says nothing about whether the broker took the order.

    So the recovery is to ask about the same client_order_id, never to send a
    second one — the single rule that keeps duplicate_order_rate at zero when the
    network is the thing that failed.
    """
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    run_id = body["run_id"]

    placed = []

    def _place(_tc, order):
        placed.append(order)
        raise TimeoutError("gateway timeout")

    _stub_broker(monkeypatch, detail={"order_status": "UNKNOWN"},
                 holdings_after=PROD_HOLDINGS, place=_place)
    main._run_order_worker(_cfg(), limit=1)

    assert len(placed) == 1
    intent = _intent(run_id)
    assert intent["status"] == "PLACING_UNKNOWN"
    assert intent["place_attempted"] is True
    assert intent["reconcile_attempts"] == 1

    # The next tick reconciles by client_order_id and does not place again.
    _stub_broker(monkeypatch, holdings_after=PROD_HOLDINGS - PROD_QUANTITY,
                 place=_place, detail={
                     "order_status": "FILLED", "filled_quantity": PROD_QUANTITY,
                     "avg_filled_price": 339.2})
    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert len(placed) == 1                                  # still exactly one
    assert result["status"] == "FILLED" and result["cashflow_finalized"] is True


def test_a_fill_the_position_has_not_shown_waits_instead_of_resending(
        tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    run_id = body["run_id"]

    placed = []
    _stub_broker(
        monkeypatch, holdings_after=PROD_HOLDINGS,       # position has not moved
        place=lambda tc, o: placed.append(o) or {"order_status": "SUBMITTED"},
        detail={"order_status": "FILLED", "filled_quantity": PROD_QUANTITY,
                "avg_filled_price": 339.2})

    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert result["status"] == main.AWAITING_FILL_CONFIRMATION
    assert result["cashflow_finalized"] is False
    assert _row(run_id)["cashflow_status"] == CASHFLOW_PENDING
    assert _row(run_id)[DELTA_COLUMN] == 0.0
    assert len(placed) == 1                                  # no second order

    # And it is still actionable, so the next tick asks the broker again.
    assert [i["run_id"] for i in _intents()] == [run_id]


def test_a_zero_quantity_fill_never_books_a_cashflow(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    _stub_broker(monkeypatch, holdings_after=PROD_HOLDINGS, detail={
        "order_status": "CANCELLED", "filled_quantity": 0})

    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert result["status"] == "CANCELLED"
    assert "cashflow_finalized" not in result
    assert _row(body["run_id"])[DELTA_COLUMN] == 0.0
    assert _row(body["run_id"])["cashflow_status"] == CASHFLOW_PENDING


def test_a_preview_that_fails_ends_the_intent_without_placing(
        tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    body, _ = _run(monkeypatch)
    placed = []
    _stub_broker(monkeypatch, detail={}, holdings_after=PROD_HOLDINGS,
                 preview=lambda tc, o: False,
                 place=lambda tc, o: placed.append(o) or {})

    result = main._run_order_worker(_cfg(), limit=1)["results"][0]

    assert result["status"] == "NOT_PLACED"
    assert "preview ไม่ผ่าน" in result["error"]
    assert placed == []


# --- F. quantity formatting, the silent 10x ----------------------------------

@pytest.mark.parametrize("qty,precision,expected", [
    (20.0, 0, "20"),
    (100.0, 0, "100"),
    (0.299, 3, "0.299"),
    (0.29949, 3, "0.299"),
    (9.14492, 5, "9.14492"),
    (1.0, 5, "1"),
])
def test_quantity_never_loses_a_digit(qty, precision, expected):
    """rstrip('0') on a string with no '.' turned 20 into 2 and 100 into 1."""
    assert quantity_string(qty, precision) == expected
    cfg = Config(symbol="AAPL", fix_c=FIX_C, decimal_precision=precision)
    assert build_order_payload(cfg, "SELL", qty, "cid")[0]["quantity"] == expected


def test_a_quantity_that_rounds_to_nothing_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="fail closed"):
        quantity_string(0.0004, 3)


# --- G. the DNA side is untouched by any of this ------------------------------

def test_dna_headroom_still_closes_the_gate_near_the_end(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    monkeypatch.setenv("LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING", "500")

    body, code = _run(monkeypatch)
    assert code == 200 and _intents() == []
    assert body["outbox_blocked_checks"] == ["dna_headroom"]


def test_production_environment_still_cannot_submit(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    monkeypatch.setenv("WEBULL_ENV", "PROD")

    body, code = _run(monkeypatch)
    assert code == 200 and _intents() == []
    assert body["outbox_blocked_checks"] == ["environment_uat"]


def test_dna_headroom_is_measured_from_the_market_ordinal(monkeypatch):
    """bypass:100 at ordinal 43 leaves 56 slots, which is why it passed."""
    assert dna_steps_remaining("bypass:100", PROD_ORDINAL) == 56
    assert math.isclose(FIX_C - PROD_HOLDINGS * PROD_PRICE, -101.50, abs_tol=0.01)
