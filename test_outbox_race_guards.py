"""Outbox transaction guards against stale execution and expiry writers."""

from datetime import datetime, timedelta, timezone

import pytest

import broker_circuit
import execution_service as execution
import lego_outbox as outbox
from conftest import FAKE_DB
from lego_one_row import Config


UTC = timezone.utc


@pytest.fixture(autouse=True)
def clean_db():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


def test_terminal_result_freezes_accounting_fields_against_stale_worker():
    outbox.put_intent("chain", "run", {"status": "PLACING_UNKNOWN"})
    finalized = outbox.update_intent("chain", "run", {
        "status": "FILLED", "filled_quantity": "1", "filled_fee": "0.05",
        "realized": True, "cashflow_finalized": True,
        "broker_cashflow_recorded": True,
    })

    stale = outbox.update_intent("chain", "run", {
        "status": "SUBMITTED", "filled_quantity": "0", "filled_fee": None,
        "realized": False, "cashflow_finalized": False,
        "broker_cashflow_recorded": False, "needs_manual_check": True,
        "audit_pending": True,
    })

    assert stale == finalized
    assert outbox.read_intent("chain", "run") == finalized


def test_terminal_allows_only_explicit_audit_repair_marker():
    outbox.put_intent("chain", "run", {"status": "PLACING_UNKNOWN"})
    terminal = outbox.update_intent("chain", "run", {
        "status": "REJECTED", "filled_quantity": "0", "reject_reason": "broker code",
    })
    revision = terminal["audit_revision"]
    repaired = outbox.update_intent("chain", "run", {"audit_pending": True})
    assert repaired["audit_revision"] == revision + 1
    assert repaired["status"] == "REJECTED"
    assert repaired["reject_reason"] == "broker code"
    assert outbox.update_intent("chain", "run", {"audit_pending": False})["audit_pending"] is False


def test_pending_fee_can_finalize_before_it_becomes_terminal():
    outbox.put_intent("chain", "run", {"status": "AWAITING_BROKER_FEE"})
    completed = outbox.update_intent("chain", "run", {
        "status": "FILLED", "filled_quantity": "1", "filled_fee": "0.05",
        "broker_fee_status": "KNOWN", "cashflow_finalized": True,
        "realized": True,
    })
    assert completed["status"] == "FILLED"
    assert completed["broker_fee_status"] == "KNOWN"
    assert completed["cashflow_finalized"] is True


def test_attempted_order_cannot_be_relabeled_unsent():
    outbox.put_intent("chain", "run", {"status": "PLACING_UNKNOWN",
                                          "place_attempted": True})
    before = outbox.read_intent("chain", "run")
    for status in outbox.UNSENT_TERMINAL:
        assert outbox.update_intent("chain", "run", {
            "status": status, "terminal_reason": "stale pre-Place result",
            "audit_pending": True,
        }) == before


def test_expire_rechecks_place_marker_inside_transaction(monkeypatch):
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {
        "status": "PENDING_DISPATCH",
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
        "claim_owner": "worker", "claim_generation": 1,
        "claim_until": (now - timedelta(seconds=1)).isoformat(),
    })
    original = outbox.list_actionable

    def raced_list(*args, **kwargs):
        candidates = original(*args, **kwargs)
        assert outbox.begin_place_attempt("chain", "run", "worker", 1)
        return candidates

    monkeypatch.setattr(outbox, "list_actionable", raced_list)
    assert outbox.expire_unsent_before("chain", now) == 0
    current = outbox.read_intent("chain", "run")
    assert current["status"] == "PLACING_UNKNOWN"
    assert current["place_attempted"] is True
    assert "expired_token" not in current


def test_expire_rechecks_extension_and_active_claim(monkeypatch):
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {
        "status": "PENDING_DISPATCH",
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
    })
    original = outbox.list_actionable

    def extended_list(*args, **kwargs):
        candidates = original(*args, **kwargs)
        outbox.update_intent("chain", "run", {
            "expires_at": (now + timedelta(minutes=1)).isoformat(),
        })
        return candidates

    monkeypatch.setattr(outbox, "list_actionable", extended_list)
    assert outbox.expire_unsent_before("chain", now) == 0
    monkeypatch.setattr(outbox, "list_actionable", original)
    outbox.update_intent("chain", "run", {
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
        "claim_owner": "worker", "claim_until": (now + timedelta(minutes=1)).isoformat(),
    })
    assert outbox.expire_unsent_before("chain", now) == 0
    assert outbox.read_intent("chain", "run")["status"] == "PENDING_DISPATCH"


def test_expire_unclaimed_intent_once_with_auditable_transition():
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {
        "status": "PENDING_DISPATCH",
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
    })
    before = outbox.read_intent("chain", "run")
    assert outbox.expire_unsent_before("chain", now) == 1
    expired = outbox.read_intent("chain", "run")
    assert expired["status"] == "EXPIRED_UNSENT"
    assert expired["audit_pending"] is True
    assert expired["audit_revision"] == before["audit_revision"] + 1
    assert expired["actionable_sort"] is None
    assert outbox.expire_unsent_before("chain", now) == 0


def test_preterminal_result_write_requires_current_claim_generation():
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {"status": "PLACING_UNKNOWN"})
    first = outbox.claim_intent("chain", "run", "old-worker",
                                now_utc=now - timedelta(seconds=3),
                                lease_seconds=1)
    successor = outbox.claim_intent("chain", "run", "new-worker",
                                    now_utc=now, lease_seconds=120)
    assert successor["claim_generation"] > first["claim_generation"]
    before = outbox.read_intent("chain", "run")
    with pytest.raises(outbox.StaleIntentClaim):
        outbox.update_intent("chain", "run", {
            "status": "SUBMITTED", "filled_quantity": "0", "realized": False,
        }, expected_claim_owner="old-worker",
            expected_claim_generation=first["claim_generation"])
    assert outbox.read_intent("chain", "run") == before

    accepted = outbox.update_intent("chain", "run", {
        "status": "SUBMITTED", "filled_quantity": "0",
    }, expected_claim_owner="new-worker",
        expected_claim_generation=successor["claim_generation"])
    assert accepted["status"] == "SUBMITTED"


def test_claim_renewal_refuses_successor_generation_and_terminal():
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {"status": "PLACING_UNKNOWN"})
    first = outbox.claim_intent("chain", "run", "old-worker",
                                now_utc=now - timedelta(seconds=3),
                                lease_seconds=1)
    successor = outbox.claim_intent("chain", "run", "new-worker",
                                    now_utc=now, lease_seconds=1)
    assert outbox.renew_intent_claim(
        "chain", "run", "old-worker", first["claim_generation"],
        now_utc=now) is None
    renewed = outbox.renew_intent_claim(
        "chain", "run", "new-worker", successor["claim_generation"],
        now_utc=now, lease_seconds=120)
    assert renewed is not None
    assert renewed["claim_generation"] == successor["claim_generation"]
    assert outbox._parse_utc(renewed["claim_until"]) > now + timedelta(minutes=1)
    outbox.update_intent("chain", "run", {"status": "FAILED", "filled_quantity": "0"})
    assert outbox.renew_intent_claim(
        "chain", "run", "new-worker", successor["claim_generation"],
        now_utc=now) is None


def test_stale_claim_cannot_reach_accounting_side_effects(monkeypatch):
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {
        "status": "PLACING_UNKNOWN", "side": "BUY", "symbol": "TSLA",
    })
    old = outbox.claim_intent("chain", "run", "old-worker",
                                  now_utc=now - timedelta(seconds=3),
                                  lease_seconds=1)
    successor = outbox.claim_intent("chain", "run", "new-worker",
                                    now_utc=now, lease_seconds=120)
    assert successor is not None

    def forbidden(*_args, **_kwargs):
        pytest.fail("stale worker reached an accounting side effect")

    monkeypatch.setattr(broker_circuit, "record_outcome", forbidden)
    monkeypatch.setattr(execution, "apply_broker_cashflow", forbidden)
    monkeypatch.setattr(execution, "apply_realized_fill", forbidden)
    monkeypatch.setattr(execution, "finalize_execution_fill", forbidden)
    result = execution._finish_with_realized(None, Config("TSLA", 100), old, {
        "status": "FILLED", "filled_quantity": "1", "filled_price": "100",
        "filled_fee": "0.05", "realized": True,
    })
    assert result == {"run_id": "run", "status": "PLACING_UNKNOWN",
                      "stale_claim": True}
    assert outbox.read_intent("chain", "run")["status"] == "PLACING_UNKNOWN"


def test_claim_lost_after_accounting_cannot_publish_stale_summary():
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {"status": "PLACING_UNKNOWN"})
    old = outbox.claim_intent("chain", "run", "old-worker",
                                  now_utc=now - timedelta(seconds=3),
                                  lease_seconds=1)
    outbox.claim_intent("chain", "run", "new-worker",
                        now_utc=now, lease_seconds=120)
    before = outbox.read_intent("chain", "run")
    with pytest.raises(outbox.StaleIntentClaim):
        execution._persist_summary(old, {
            "status": "PARTIAL_FILLED", "filled_quantity": "1",
            "filled_price": "100", "realized": True,
        })
    assert outbox.read_intent("chain", "run") == before


def test_stale_preview_metadata_cannot_replace_successor_evidence():
    now = datetime.now(UTC)
    outbox.put_intent("chain", "run", {"status": "PENDING_DISPATCH"})
    old = outbox.claim_intent("chain", "run", "old-worker",
                                  now_utc=now - timedelta(seconds=3),
                                  lease_seconds=1)
    successor = outbox.claim_intent("chain", "run", "new-worker",
                                    now_utc=now, lease_seconds=120)
    outbox.update_intent("chain", "run", {
        "preview_result": {"estimated_cost": "100"},
        "funding_check": {"buying_power": "200"},
        "state": "PREVIEWED",
    }, expected_claim_owner="new-worker",
        expected_claim_generation=successor["claim_generation"])
    before = outbox.read_intent("chain", "run")
    with pytest.raises(outbox.StaleIntentClaim):
        outbox.update_intent("chain", "run", {
            "preview_result": {"estimated_cost": "999"},
            "funding_check": {"buying_power": "1"},
            "state": "PREVIEWED",
        }, **execution._claim_update_kwargs(old))
    assert outbox.read_intent("chain", "run") == before


def test_fenced_recovery_cannot_regress_a_newer_broker_status():
    outbox.put_intent("chain", "run", {"status": "PENDING_DISPATCH"})
    outbox.update_intent("chain", "run", {
        "status": "SUBMITTED", "place_attempted": True,
        "filled_quantity": "0",
    })
    before = outbox.read_intent("chain", "run")
    assert outbox.recover_fenced_intent("chain", "run") == before
    assert outbox.read_intent("chain", "run") == before

    outbox.put_intent("chain", "unsent", {"status": "PENDING_DISPATCH"})
    recovered = outbox.recover_fenced_intent("chain", "unsent")
    assert recovered["status"] == "PLACING_UNKNOWN"
    assert recovered["place_attempted"] is True
