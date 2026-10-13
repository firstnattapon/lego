"""2026-10-12: releasing the stuck order is not enough while the broker still lists it.

Both dispatch guards (before Preview and again after it) stop every new order with
SUPPRESSED_ACTIVE_ORDER when the broker lists any open order for the symbol. UAT never retires
an order it left PENDING, so after the UAT stuck-PENDING release (test_incident_20261012.py)
the released order is still listed and, unchanged, would block every later slot for the rest of
the session -- the release would only move the stop from our fence to the broker's list.

So the guards ignore a listed row exactly while it is what we released: an intent of this chain
that ended EXPIRED / uat_stuck_pending, and the broker still says PENDING with nothing filled.
Anything else -- another order, a changed row, an unreadable intent, and every row on PROD --
blocks as before. These run the real dispatch (fractional gate, Preview, funding, submit gate,
database fences) with broker doubles, as test_dispatch_overshoot.py does.
"""
from __future__ import annotations

import pytest

import execution_service as execution
import order_recovery as recovery
from conftest import FAKE_DB
from lego_outbox import OUTBOX_PATH, account_symbol_fence_key, claim_chain_dispatch, read_intent
from test_dispatch_overshoot import CFG, NOW, dispatch_fixture, isolate  # noqa: F401  (isolate is autouse)

ZOMBIE = "b" * 32


def released(chain, **changes):
    """The intent the UAT rule leaves behind (what _persist_summary wrote on release)."""
    doc = {"run_id": ZOMBIE, "chain_key": chain, "symbol": "PFE", "side": "BUY", "quantity": 1.37,
           "status": "EXPIRED", "broker_status": "PENDING", "place_attempted": True,
           "filled_quantity": "0.000000", "expiry_released": True,
           "expiry_basis": recovery.BASIS_UAT_STUCK_PENDING,
           "terminal_reason": recovery.UAT_STUCK_PENDING_REASON}
    doc.update(changes)
    FAKE_DB.reference(f"{OUTBOX_PATH}/{chain}/{ZOMBIE}").set(doc)
    return doc


def row(**changes):
    doc = {"client_order_id": ZOMBIE, "symbol": "PFE", "side": "BUY", "status": "PENDING",
           "filled_quantity": "0", "total_quantity": "1.37", "order_type": "MARKET",
           "time_in_force": "DAY"}
    doc.update(changes)
    return doc


def listing(monkeypatch, *answers):
    """fetch_open_orders answers once per call, then keeps repeating the last answer."""
    pending = list(answers)

    def fetch(*_):
        return list(pending.pop(0) if len(pending) > 1 else pending[0])

    monkeypatch.setattr(execution, "fetch_open_orders", fetch)


def dispatch(fixture):
    """Dispatch under the lock the worker really holds: the account/symbol fence."""
    runtime, intent, _fixture_claim, client, _ = fixture
    scope = account_symbol_fence_key(execution.runtime_identity_fingerprint(), CFG.symbol)
    claim = claim_chain_dispatch(scope, "test-worker", now_utc=NOW)
    claim["dispatch_scope_key"] = scope                 # what _run_order_worker adds
    return execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime), \
        intent, client


# ---------------------------------------------------------------- the point of the change
def test_a_released_stuck_order_that_the_broker_still_lists_does_not_block_the_next_order(monkeypatch):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [row()])                       # both guards see the released order
    result, intent, client = dispatch(fixture)
    assert result["status"] == "SUBMITTED"
    assert len(client.order_v3.preview_order.calls) == len(client.order_v3.place_order.calls) == 1
    stored = read_intent(intent["chain_key"], intent["run_id"])
    assert stored["place_attempted"] is True and stored["status"] != "SUPPRESSED_ACTIVE_ORDER"
    warning = FAKE_DB.reference("webull_lego_warnings/uat_stuck_pending_ignored").get()
    assert warning["ignored_released"] == 1 and warning["ignored_client_order_ids"] == [ZOMBIE]
    assert not FAKE_DB.reference("webull_lego_order_dispatch_locks").get() or not any(
        (doc or {}).get("broker_open_order_blocker")
        for doc in FAKE_DB.reference("webull_lego_order_dispatch_locks").get().values())


def test_the_same_listing_blocks_on_prod(monkeypatch):
    fixture = dispatch_fixture(monkeypatch, environment="PROD")
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [row()])
    result, intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER" and result["open_order_count"] == 1
    assert client.order_v3.preview_order.calls == [] and client.order_v3.place_order.calls == []
    assert not FAKE_DB.reference("webull_lego_warnings/uat_stuck_pending_ignored").get()


# ------------------------------------------------------------ only what we released is ignored
@pytest.mark.parametrize("what,other", [
    ("another order", row(client_order_id="c" * 32)),
    ("not ours at all", row(client_order_id="d" * 32, symbol="PFE", status="SUBMITTED")),
])
def test_any_other_open_order_still_blocks_even_beside_a_released_one(monkeypatch, what, other):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [row(), other])
    result, _intent, client = dispatch(fixture)
    # The released order is ignored; only the other one is counted and blocks.
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER" and result["open_order_count"] == 1, what
    assert client.order_v3.preview_order.calls == [] and client.order_v3.place_order.calls == []


@pytest.mark.parametrize("what,changes", [
    ("working", dict(status="WORKING")),
    ("submitted", dict(status="SUBMITTED")),
    ("partly filled", dict(status="PARTIAL_FILLED", filled_quantity="0.4")),
    ("pending but filled", dict(filled_quantity="0.01")),
    ("fill unknown", dict(filled_quantity=None)),
    ("fill malformed", dict(filled_quantity="n/a")),
    ("other symbol", dict(symbol="AAPL")),
])
def test_the_released_order_blocks_again_the_moment_the_broker_row_changes(monkeypatch, what, changes):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [row(**changes)])
    result, _intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER", what
    assert client.order_v3.preview_order.calls == [] and client.order_v3.place_order.calls == []


@pytest.mark.parametrize("what,changes", [
    ("still held", dict(status="CANCEL_UNKNOWN", expiry_released=None, expiry_basis=None)),
    ("manual", dict(status="MANUAL_RECONCILIATION_REQUIRED", expiry_released=None, expiry_basis=None)),
    ("released by the original proof", dict(expiry_basis=recovery.BASIS_SESSION_OVER)),
    ("no basis recorded", dict(expiry_basis=None)),
    ("not released", dict(expiry_released=False)),
    ("filled after all", dict(status="FILLED", broker_status="FILLED")),
    ("broker said expired", dict(broker_status="EXPIRED")),
    ("another symbol", dict(symbol="AAPL")),
])
def test_only_an_order_the_uat_rule_released_is_ignored(monkeypatch, what, changes):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"], **changes)
    listing(monkeypatch, [row()])
    result, _intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER", what
    assert client.order_v3.place_order.calls == []


def test_an_intent_that_cannot_be_read_blocks(monkeypatch):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [row()])

    def broken(*_):
        raise TimeoutError("database unavailable")

    monkeypatch.setattr(execution, "read_intent", broken)
    result, _intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER"
    assert client.order_v3.place_order.calls == []


def test_a_row_without_a_client_order_id_blocks(monkeypatch):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [row(client_order_id="")])
    result, _intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER"
    assert client.order_v3.place_order.calls == []


# --------------------------------------- the second look, after Preview (a different race)
def test_after_preview_a_released_order_is_ignored_and_anything_new_blocks(monkeypatch):
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [], [row()])                    # nothing before Preview, the released one after
    result, _intent, client = dispatch(fixture)
    assert result["status"] == "SUBMITTED"
    assert len(client.order_v3.preview_order.calls) == len(client.order_v3.place_order.calls) == 1

    FAKE_DB.store.clear()
    fixture = dispatch_fixture(monkeypatch)
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [], [row(), row(client_order_id="c" * 32)])   # an order appeared meanwhile
    result, intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER" and result["open_order_count"] == 1
    assert len(client.order_v3.preview_order.calls) == 1 and client.order_v3.place_order.calls == []
    assert read_intent(intent["chain_key"], intent["run_id"])["dispatch_check_phase"] == "post_preview"


def test_after_preview_the_same_listing_blocks_on_prod(monkeypatch):
    fixture = dispatch_fixture(monkeypatch, environment="PROD")
    released(fixture[1]["chain_key"])
    listing(monkeypatch, [], [row()])
    result, intent, client = dispatch(fixture)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER"
    assert len(client.order_v3.preview_order.calls) == 1 and client.order_v3.place_order.calls == []
    assert read_intent(intent["chain_key"], intent["run_id"])["dispatch_check_phase"] == "post_preview"


# ------------------------------------------------------------------------- the splitter
def test_the_splitter_keeps_order_and_returns_everything_as_blocking_off_uat(monkeypatch):
    chain = dispatch_fixture(monkeypatch)[1]["chain_key"]
    released(chain)
    other = row(client_order_id="c" * 32)
    blocking, ignored = execution._split_released_stuck(chain, [other, row()])
    assert blocking == [other] and ignored == [row()]
    assert execution._split_released_stuck(chain, []) == ([], [])
    monkeypatch.setattr(execution, "environment_label", lambda: "PROD")
    blocking, ignored = execution._split_released_stuck(chain, [other, row()])
    assert blocking == [other, row()] and ignored == []
