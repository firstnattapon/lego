"""Why `จำนวนถือครอง (หุ้น)` read 8.78392 on all 37 rows of the export.

2026-07-29 16:25Z → 2026-07-30 18:45Z, AAPL, DNA step 63 → 99, market clock, one
row per 15-minute slot with no gap. Thirty-three of those rows are
TRIGGER_ACTION — twelve READY_SELL, twenty-one READY_BUY — and every one of them
carries `cashflow_status=PENDING_EXECUTION`. Not one order was ever sent, so the
broker position never moved and the column repeated 8.78392 for a day and a half.
ΔAₙ/Aₙ/Eₙ are 0.0 on all 37 rows for the same reason: nothing filled.

The Cloud Logging export of the same window names the block outright::

    lego warning kind=auto_submit_blocked token ยังไม่พร้อม: … ไม่พบ token file
    ที่ /tmp/webull_token/token.txt … {'row_status': 'READY_BUY',
    'clock_mode': 'market', 'blocked_by': ['token_ready']}

…and, on every SDK call in the same window, the reason there was no file::

    webull.core.http.initializer.client_initializer INFO
    _check_token_enable result is False

`ClientInitializer.init_token` asks the broker whether token checking is enabled
and returns before `TokenManager` is ever constructed when the answer is no.
On this UAT app the answer is always no: token.txt is never written, never will
be, and the SDK signs with HMAC alone — which is why the very same invocation
went on to read the account position successfully (the only errors in the window
are transient `GATEWAY_TIMEOUT` 504s on `/openapi/assets/positions`, which
`_retry_transient` recovered from — every slot in the export committed).

`token_health()` scored that missing file as "nothing to sign with", so
`auto_submit_preflight` refused the token and `lego_one_row` committed a
READY_BUY/READY_SELL row with `outbox_blocked=['token_ready']` on every slot.

These tests pin both halves:

* the decision math is bit-identical to the export — the engine was never wrong,
  and this fix must not move a single column;
* every TRIGGER_ACTION row of that export now becomes an order intent, reaches
  the broker, and moves the position.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
import webull_io
from conftest import FAKE_DB
from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN,
                          REFERENCE_COLUMN, Anchor, columns_presented,
                          compute_row)
from lego_outbox import OUTBOX_PATH, list_actionable
from lego_state import CASHFLOW_FINALIZED, chain_key
from market_clock import resolve_market_slot

UTC = timezone.utc

# The deployed revision of the incident window, unchanged.
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

FIX_C = 3000.0
# The one number this whole file is about: identical on every row of the export.
FROZEN_HOLDINGS = 8.78392
# Rₙ = 0 on row 0 at 341.00, so the chain's p0 is 341.00 and — because nothing
# ever acted — P_acted never moved off it either.
P0 = 341.0

# (captured_at, ordinal, price, status, quantity, value, gap, R) straight from
# 20260802T0614_export.csv. Money columns are the exported 2-dp presentation.
EXPORT = (
    ("2026-07-29T16:25:07Z", 63, 341.00, "PASS_THRESHOLD", 0.000, 2995.32, 4.68, 0.00),
    ("2026-07-29T16:30:13Z", 64, 340.78, "READY_BUY", 0.019, 2993.38, 6.62, -1.94),
    ("2026-07-29T16:45:23Z", 65, 341.20, "PASS_THRESHOLD", 0.000, 2997.07, 2.93, 1.76),
    ("2026-07-29T17:00:12Z", 66, 340.54, "READY_BUY", 0.026, 2991.28, 8.72, -4.05),
    ("2026-07-29T17:15:14Z", 67, 341.32, "PASS_THRESHOLD", 0.000, 2998.17, 1.83, 2.86),
    ("2026-07-29T17:30:15Z", 68, 342.26, "READY_SELL", 0.019, 3006.41, -6.41, 11.09),
    ("2026-07-29T17:45:13Z", 69, 342.63, "READY_SELL", 0.028, 3009.63, -9.63, 14.31),
    ("2026-07-29T18:00:14Z", 70, 343.09, "READY_SELL", 0.040, 3013.68, -13.68, 18.33),
    ("2026-07-29T18:15:14Z", 71, 343.28, "READY_SELL", 0.045, 3015.34, -15.34, 19.99),
    ("2026-07-29T18:30:17Z", 72, 343.74, "READY_SELL", 0.056, 3019.38, -19.38, 24.01),
    ("2026-07-29T18:45:12Z", 73, 343.62, "READY_SELL", 0.053, 3018.33, -18.33, 22.96),
    ("2026-07-29T19:00:14Z", 74, 343.66, "READY_SELL", 0.054, 3018.68, -18.68, 23.31),
    ("2026-07-29T19:15:13Z", 75, 342.87, "READY_SELL", 0.034, 3011.74, -11.74, 16.41),
    ("2026-07-29T19:30:17Z", 76, 342.34, "READY_SELL", 0.021, 3007.04, -7.04, 11.72),
    ("2026-07-29T19:45:16Z", 77, 341.49, "PASS_THRESHOLD", 0.000, 2999.62, 0.38, 4.31),
    ("2026-07-30T13:30:12Z", 78, 338.19, "READY_BUY", 0.087, 2970.63, 29.37, -24.82),
    ("2026-07-30T13:45:16Z", 79, 332.80, "READY_BUY", 0.231, 2923.29, 76.71, -73.02),
    ("2026-07-30T14:00:14Z", 80, 333.10, "READY_BUY", 0.222, 2925.92, 74.08, -70.32),
    ("2026-07-30T14:15:13Z", 81, 331.78, "READY_BUY", 0.258, 2914.33, 85.67, -82.23),
    ("2026-07-30T14:30:14Z", 82, 331.27, "READY_BUY", 0.272, 2909.85, 90.15, -86.85),
    ("2026-07-30T14:45:14Z", 83, 332.12, "READY_BUY", 0.249, 2917.32, 82.68, -79.16),
    ("2026-07-30T15:00:15Z", 84, 330.58, "READY_BUY", 0.291, 2903.79, 96.21, -93.10),
    ("2026-07-30T15:15:14Z", 85, 331.66, "READY_BUY", 0.262, 2913.23, 86.77, -83.36),
    ("2026-07-30T15:30:14Z", 86, 331.91, "READY_BUY", 0.255, 2915.47, 84.53, -81.06),
    ("2026-07-30T15:45:15Z", 87, 331.80, "READY_BUY", 0.258, 2914.55, 85.45, -82.01),
    ("2026-07-30T16:00:15Z", 88, 331.83, "READY_BUY", 0.257, 2914.77, 85.23, -81.78),
    ("2026-07-30T16:15:13Z", 89, 331.92, "READY_BUY", 0.254, 2915.56, 84.44, -80.97),
    ("2026-07-30T16:30:12Z", 90, 331.95, "READY_BUY", 0.254, 2915.82, 84.18, -80.69),
    ("2026-07-30T16:45:26Z", 91, 331.54, "READY_BUY", 0.265, 2912.22, 87.78, -84.40),
    ("2026-07-30T17:00:14Z", 92, 332.07, "READY_BUY", 0.250, 2916.88, 83.12, -79.61),
    ("2026-07-30T17:15:23Z", 93, 331.70, "READY_BUY", 0.260, 2913.67, 86.33, -82.91),
    ("2026-07-30T17:30:22Z", 94, 332.64, "READY_BUY", 0.235, 2921.93, 78.07, -74.42),
    ("2026-07-30T17:45:17Z", 95, 332.11, "READY_BUY", 0.249, 2917.23, 82.77, -79.25),
    ("2026-07-30T18:00:24Z", 96, 332.65, "READY_BUY", 0.235, 2921.97, 78.03, -74.37),
    ("2026-07-30T18:15:14Z", 97, 333.36, "READY_BUY", 0.215, 2928.21, 71.79, -67.98),
    ("2026-07-30T18:30:23Z", 98, 333.46, "READY_BUY", 0.213, 2929.09, 70.91, -67.08),
    ("2026-07-30T18:45:18Z", 99, 333.02, "READY_BUY", 0.225, 2925.22, 74.78, -71.04),
)
TRIGGERED = tuple(r for r in EXPORT if r[3] != "PASS_THRESHOLD")
# Step 99 is the last index bypass:100 has, so `dna_headroom` closes it on its
# own merits — the one triggered row of the export that must still send nothing.
LAST_DNA_STEP = 99
TRIGGERED_WITH_HEADROOM = tuple(r for r in TRIGGERED if r[1] != LAST_DNA_STEP)
# The first blocked decision of the window; used wherever one row is enough.
FIRST_BLOCKED = EXPORT[1]

# `ราคา Pₙ (USD)` is a money column, so the export carries it rounded to 2 dp
# while the engine decided on the full-precision quote. Both bounds below are
# that rounding, derived rather than tuned until the suite went green:
#
#   value  = holdings × price, so a price off by at most 0.005 moves it by
#            0.005 × 8.78392 = 0.0439, and the value column is itself rounded to
#            2 dp on each side: 0.0439 + 0.005 + 0.005 = 0.0539.
#   qty    = |gap| ÷ price rounded to LEGO_DECIMAL_PRECISION=3, so the same
#            0.0439 of gap can carry it across one boundary: one ulp = 0.001.
#
# Seven of the thirty-seven rows sit outside exact equality and none outside
# these; the status, the step, the gate and the three cashflow columns are exact
# on all thirty-seven. The extra 1e-3/1e-4 is float representation slack — the
# difference at row 76 is 0.050000000000018.
EXPORT_MONEY_TOLERANCE = 0.055
EXPORT_QUANTITY_TOLERANCE = 0.0011


def _moment(captured_at: str) -> datetime:
    return datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _fixed_now(moment: datetime):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return _Now


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """The deployed environment, and a token dir the SDK never writes into.

    An existing, writable, empty directory is exactly what production had: the
    SDK creates the directory in TokenStorage.__init__ but only writes token.txt
    after a 2FA flow that `_check_token_enable result is False` skips entirely.
    """
    FAKE_DB.store.clear()
    webull_io.reset_clients()
    for key, value in PROD_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("LEGO_INLINE_ORDER_WORKER", "LEGO_DNA_LOW_WATERMARK",
                "LEGO_MARKET_HOLIDAYS", "LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING",
                "LEGO_ALLOW_EPHEMERAL_TOKEN_DIR", "LEGO_FILL_CONFIRM_MAX_ATTEMPTS"):
        monkeypatch.delenv(key, raising=False)
    token_dir = tmp_path / "webull_token"
    token_dir.mkdir()
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(token_dir))
    assert webull_io.token_dir_is_ephemeral() is True     # /tmp, as on Cloud Run
    assert webull_io.read_local_token() is None           # never written
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "ORDER_POLL_DELAY_S", 0.0)


def _cfg():
    return main.load_config()


def _run(monkeypatch, row, holdings=FROZEN_HOLDINGS):
    """One slot of the export through the real pipeline."""
    captured_at, price = row[0], row[2]
    moment = _moment(captured_at)
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": captured_at, "quote_time": captured_at,
        "price": price, "holdings": holdings})
    return main.lego_one_row(object())


def _intent(run_id):
    return FAKE_DB.reference(f"{OUTBOX_PATH}/{chain_key(_cfg())}/{run_id}").get()


def _stub_broker(monkeypatch, *, detail, holdings_after, place=None, preview=True):
    monkeypatch.setattr(main, "preview_market_order",
                        preview if callable(preview) else (lambda tc, o: preview))
    monkeypatch.setattr(main, "fetch_open_orders", lambda tc, s: [])
    monkeypatch.setattr(main, "place_market_order",
                        place or (lambda tc, o: {"order_status": "SUBMITTED"}))
    monkeypatch.setattr(main, "fetch_order_detail",
                        detail if callable(detail) else (lambda tc, r: detail))
    monkeypatch.setattr(main, "fetch_holdings",
                        holdings_after if callable(holdings_after)
                        else (lambda tc, cfg: float(holdings_after)))


# --- A. the export the fix must not disturb ----------------------------------

def test_the_whole_export_recomputes_from_its_own_numbers(monkeypatch):
    """Thirty-seven rows, decision columns and Rₙ, against the exported values.

    The engine was never the fault here, so this is the guard on goal 2: if any
    of it moves, the fix has changed the strategy rather than unblocked it. The
    status, the step, the gate and the three cashflow columns are exact; the
    money columns are checked against the rounding the export itself applied to
    the price (see EXPORT_MONEY_TOLERANCE).
    """
    cfg = _cfg()
    anchor = Anchor(version=1, dna_step=62, p0=P0, prev_price=P0,
                    prev_actual=0.0, prev_holdings=FROZEN_HOLDINGS)

    for captured_at, ordinal, price, status, quantity, value, gap, R in EXPORT:
        moment = _moment(captured_at)
        monkeypatch.setattr(main, "datetime", _fixed_now(moment))
        slot = resolve_market_slot(moment)
        assert slot.market_ordinal == ordinal          # the clock, independently

        row = columns_presented(compute_row(
            cfg, {"captured_at": captured_at, "price": price,
                  "holdings": FROZEN_HOLDINGS},
            anchor, dna_step=slot.market_ordinal))

        assert row["DNA step"] == ordinal
        assert row["DNA signal"] == 1                  # bypass:100 opens every slot
        assert row["สถานะ"] == status
        assert row["จำนวนถือครอง (หุ้น)"] == FROZEN_HOLDINGS
        assert row["จำนวนสั่ง (หุ้น)"] == pytest.approx(
            quantity, abs=EXPORT_QUANTITY_TOLERANCE)
        assert row["มูลค่าพอร์ต (USD)"] == pytest.approx(
            value, abs=EXPORT_MONEY_TOLERANCE)
        assert row["ส่วนต่างเป้าหมาย (USD)"] == pytest.approx(
            -gap, abs=EXPORT_MONEY_TOLERANCE)
        assert row[REFERENCE_COLUMN] == pytest.approx(
            R, abs=EXPORT_MONEY_TOLERANCE)
        # A decision is not an execution: nothing filled, so all three stay 0.0
        # — exactly as the export has them on all 37 rows.
        assert row[DELTA_COLUMN] == row[ACTUAL_COLUMN] == row[EXCESS_COLUMN] == 0.0


def test_the_exported_column_really_never_moved():
    """The premise, asserted rather than asserted-about."""
    assert len({r[0][:10] for r in EXPORT}) == 2       # two trading days
    assert [r[1] for r in EXPORT] == list(range(63, 100))   # no slot skipped
    assert len(TRIGGERED) == 33
    assert sum(1 for r in TRIGGERED if r[3] == "READY_SELL") == 9
    assert sum(1 for r in TRIGGERED if r[3] == "READY_BUY") == 24
    # The holdings column is a single constant here because that is what the
    # export is: one number on all 37 rows, so there is no per-row value to
    # carry. 33 decisions to trade and the position never once answered.
    assert TRIGGERED[0][1] == 64 and TRIGGERED[-1][1] == LAST_DNA_STEP


# --- B. the block, and its removal -------------------------------------------

def test_the_missing_token_file_is_exactly_what_shut_the_gate(monkeypatch):
    """token_health's own reading of the production token dir.

    One reason about the *next* container and one about a file the broker never
    asks this app for. Neither is a verdict on the token in hand, and the second
    is what `ready` used to turn into a permanent block.
    """
    health = webull_io.token_health()

    assert health["found"] is False
    assert health["ok"] is False and health["ready"] is False
    assert health["durability_risk_only"] is False     # the file reason is not that
    assert health["live_proof_supersedable"] is True
    assert len(health["reasons"]) == 2
    assert "ไม่พบ token file" in health["reasons"][1]


@pytest.mark.parametrize("row", TRIGGERED_WITH_HEADROOM,
                         ids=lambda r: f"step{r[1]}-{r[3]}")
def test_every_triggered_row_of_the_export_now_becomes_an_order_intent(
        monkeypatch, row):
    """32 rows, 32 intents. The export produced zero.

    Each runs on a fresh chain at its own slot, so the ordinal, the side and the
    quantity are the exported ones and not a value carried over from the row
    before.
    """
    _captured_at, ordinal, _price, status, quantity, _value, _gap, _R = row
    body, code = _run(monkeypatch, row)

    assert code == 200 and body["committed"] is True
    assert body["status"] == status
    assert body["step"] == body["market_step"] == ordinal
    assert "outbox_blocked" not in body and "outbox_blocked_checks" not in body
    assert FAKE_DB.reference(
        "webull_lego_warnings/auto_submit_blocked").get() is None

    intent = _intent(body["run_id"])
    assert intent["status"] == "PENDING_DISPATCH"
    assert intent["side"] == ("BUY" if status == "READY_BUY" else "SELL")
    assert intent["quantity"] == pytest.approx(
        quantity, abs=EXPORT_QUANTITY_TOLERANCE)
    assert intent["decision_holdings"] == FROZEN_HOLDINGS


def test_the_last_dna_step_of_the_export_is_still_blocked_but_not_by_the_token(
        monkeypatch):
    """Step 99 of bypass:100 has no headroom left, and that block is correct.

    Worth pinning separately: the export's final row was blocked by *two* checks
    — the log line reads `blocked_by: ['token_ready', 'dna_headroom']` — and only
    the first of them was a fault. Removing it must leave the second standing.
    """
    row = TRIGGERED[-1]
    assert row[1] == LAST_DNA_STEP
    body, code = _run(monkeypatch, row)

    assert code == 200 and body["committed"] is True
    assert body["status"] == "READY_BUY"
    assert body["outbox_blocked_checks"] == ["dna_headroom"]
    assert body["dna_steps_remaining"] == 0
    assert list_actionable(chain_key(_cfg())) == []


def test_the_missing_file_is_still_reported_on_every_slot(monkeypatch):
    """Unblocked is not unsaid: the operator still gets the warning."""
    body, _ = _run(monkeypatch, FIRST_BLOCKED)

    assert "ไม่พบ token file" in body["token_warning"]
    warning = FAKE_DB.reference("webull_lego_warnings/webull_token").get()
    assert warning["count"] == 1 and warning["ephemeral_token_dir"] is True


def test_the_pass_rows_of_the_export_still_send_nothing(monkeypatch):
    """|gap| <= DIFF is not a decision, and the fix must not make it one."""
    for row in (r for r in EXPORT if r[3] == "PASS_THRESHOLD"):
        FAKE_DB.store.clear()
        body, code = _run(monkeypatch, row)
        assert code == 200 and body["status"] == "PASS_THRESHOLD"
        assert list_actionable(chain_key(_cfg())) == []
        assert "outbox_blocked" not in body


# --- C. the column finally moves ---------------------------------------------

def test_the_position_moves_and_the_ledger_follows(monkeypatch):
    """Step 64: READY_BUY 0.019 at 340.78 — the first order the export never sent.

    Runs the whole delivery path on the intent the fix creates and checks the
    thing the incident is named after: `จำนวนถือครอง (หุ้น)` is a different
    number afterwards.
    """
    captured_at, ordinal, price, status, quantity = FIRST_BLOCKED[:5]
    body, _ = _run(monkeypatch, FIRST_BLOCKED)
    run_id = body["run_id"]
    assert (body["status"], body["step"]) == (status, ordinal)

    fill_price = 340.80
    holdings_after = round(FROZEN_HOLDINGS + quantity, 5)      # 8.80292
    placed = []

    def _place(_tc, order):
        placed.append(order)
        return {"order_status": "SUBMITTED"}

    _stub_broker(monkeypatch, place=_place, holdings_after=holdings_after,
                 detail={"order_status": "FILLED", "filled_quantity": quantity,
                         "avg_filled_price": fill_price, "transaction_fee": 0.0})

    result = main._run_order_worker(_cfg(), limit=3)["results"][0]

    # The order went out once, under the row's own run_id.
    assert len(placed) == 1
    assert placed[0][0]["client_order_id"] == run_id
    assert placed[0][0]["side"] == "BUY"
    assert placed[0][0]["quantity"] == "0.019"

    # And the position is no longer 8.78392.
    assert result["status"] == "FILLED"
    assert result["cashflow_finalized"] is True
    assert result["post_execution_holdings"] == holdings_after
    assert holdings_after != FROZEN_HOLDINGS

    # ΔAₙ = fix_c × (filled/P_acted − 1), booked from the executed price. The
    # export had 0.0 here on every row because no fill ever reached this line.
    row = FAKE_DB.reference(f"webull_lego_rows/{run_id}").get()
    assert row["cashflow_status"] == CASHFLOW_FINALIZED
    assert row[DELTA_COLUMN] == pytest.approx(
        FIX_C * (fill_price / price - 1.0), abs=1e-9)
    assert row[ACTUAL_COLUMN] == pytest.approx(row[DELTA_COLUMN], abs=1e-9)

    # The next slot reads the moved position straight from the broker.
    FAKE_DB.reference("webull_lego_warnings").delete()
    following = EXPORT[2]
    body_next, code_next = _run(monkeypatch, following, holdings=holdings_after)
    assert code_next == 200
    assert FAKE_DB.reference(
        f"webull_lego_rows/{body_next['run_id']}").get()[
            "จำนวนถือครอง (หุ้น)"] == holdings_after
