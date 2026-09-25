"""Operator stop and audit replay regressions; no live broker I/O."""
from datetime import datetime, timezone

import pytest

import lego_outbox as outbox
import operator_halt
from conftest import FAKE_DB, FakeReference


IDENTITY = "operator-halt-test-account"
SYMBOL = "TSLA"
SCOPE = outbox.account_symbol_fence_key(IDENTITY, SYMBOL)
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def empty_database():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


def test_halt_blocks_new_fence_and_requires_second_operator_to_resume():
    dry = operator_halt.set_halt(IDENTITY, SYMBOL, operator="alice",
                                 reason="risk review")
    assert dry["dry_run"] is True
    assert operator_halt.status(IDENTITY, SYMBOL) == {}

    applied = operator_halt.set_halt(
        IDENTITY, SYMBOL, operator="alice", reason="risk review", apply=True)
    assert applied["inflight"] is False
    assert operator_halt.status(IDENTITY, SYMBOL)["halted"] is True
    assert operator_halt.set_halt(
        IDENTITY, SYMBOL, operator="alice", reason="repeat", apply=True)["halt_id"] \
        == applied["halt_id"]
    claim = outbox.claim_chain_dispatch(SCOPE, "worker", now_utc=NOW)
    assert outbox.fence_chain_dispatch(
        SCOPE, "new-run", "worker", claim["claim_token"], now_utc=NOW) is None
    with pytest.raises(ValueError, match="second operator"):
        operator_halt.clear_halt(
            IDENTITY, SYMBOL, expected_halt_id=applied["halt_id"],
            operator="alice", reason="reviewed")
    with pytest.raises(ValueError, match="worker active"):
        operator_halt.clear_halt(
            IDENTITY, SYMBOL, expected_halt_id=applied["halt_id"],
            operator="bob", reason="reviewed")
    outbox.release_chain_dispatch(SCOPE, "worker", claim["claim_token"])
    assert operator_halt.clear_halt(
        IDENTITY, SYMBOL, expected_halt_id=applied["halt_id"],
        operator="bob", reason="reviewed")["dry_run"] is True
    operator_halt.clear_halt(
        IDENTITY, SYMBOL, expected_halt_id=applied["halt_id"],
        operator="bob", reason="reviewed", apply=True)
    assert operator_halt.status(IDENTITY, SYMBOL)["halted"] is False
    audit = FAKE_DB.reference(f"{operator_halt.AUDIT_PATH}/{SCOPE}").get()
    assert {event["action"] for event in audit.values()} == {"HALT", "CLEAR"}


def test_halt_reports_existing_inflight_and_preserves_reconciliation_fence():
    claim = outbox.claim_chain_dispatch(SCOPE, "worker", now_utc=NOW)
    assert outbox.fence_chain_dispatch(
        SCOPE, "existing", "worker", claim["claim_token"], now_utc=NOW)
    applied = operator_halt.set_halt(
        IDENTITY, SYMBOL, operator="alice", reason="emergency", apply=True)
    assert applied["inflight"] is True
    assert FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{SCOPE}").get()[
        "inflight_run_id"] == "existing"
    with pytest.raises(ValueError, match="unresolved order fence"):
        operator_halt.clear_halt(
            IDENTITY, SYMBOL, expected_halt_id=applied["halt_id"],
            operator="bob", reason="reviewed")
    assert outbox.clear_chain_dispatch_inflight(
        SCOPE, "existing", "worker", claim["claim_token"])
    assert outbox.fence_chain_dispatch(
        SCOPE, "next", "worker", claim["claim_token"], now_utc=NOW) is None


def test_failed_audit_mirror_keeps_halt_and_blocks_resume(monkeypatch):
    original = FakeReference.transaction
    fail_once = [True]

    def interrupted(self, callback):
        if self.parts and self.parts[0] == operator_halt.AUDIT_PATH and fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("audit write interrupted")
        return original(self, callback)

    monkeypatch.setattr(FakeReference, "transaction", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        operator_halt.set_halt(
            IDENTITY, SYMBOL, operator="alice", reason="emergency", apply=True)
    current = operator_halt.status(IDENTITY, SYMBOL)
    assert current["halted"] is True and current["audit_pending_event"]
    with pytest.raises(ValueError, match="audit is pending"):
        operator_halt.clear_halt(
            IDENTITY, SYMBOL, expected_halt_id=current["halt_id"],
            operator="bob", reason="reviewed")
    assert operator_halt.repair_audit(IDENTITY, SYMBOL) is True
    assert operator_halt.repair_audit(IDENTITY, SYMBOL) is False
    assert not operator_halt.status(IDENTITY, SYMBOL).get("audit_pending_event")


def test_clear_audit_failure_blocks_next_dispatch_until_repaired(monkeypatch):
    applied = operator_halt.set_halt(
        IDENTITY, SYMBOL, operator="alice", reason="risk review", apply=True)
    original = FakeReference.transaction
    fail_once = [True]

    def interrupted(self, callback):
        if self.parts and self.parts[0] == operator_halt.AUDIT_PATH and fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("clear audit interrupted")
        return original(self, callback)

    monkeypatch.setattr(FakeReference, "transaction", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        operator_halt.clear_halt(
            IDENTITY, SYMBOL, expected_halt_id=applied["halt_id"],
            operator="bob", reason="reviewed", apply=True)
    current = operator_halt.status(IDENTITY, SYMBOL)
    assert current["halted"] is False and current["audit_pending_event"]
    claim = outbox.claim_chain_dispatch(SCOPE, "worker", now_utc=NOW)
    assert outbox.fence_chain_dispatch(
        SCOPE, "new-run", "worker", claim["claim_token"], now_utc=NOW) is None
    assert operator_halt.repair_audit(IDENTITY, SYMBOL) is True
    assert outbox.fence_chain_dispatch(
        SCOPE, "new-run", "worker", claim["claim_token"], now_utc=NOW)
