"""Reviewed re-entry preserves both the money fence and operator halt."""
import pytest
from conftest import FAKE_DB
import lego_outbox as outbox
import order_recovery
from tools import resume_order_reconciliation as resume
from test_continuous_recovery_v4 import isolated
from test_incident_20260928 import fractional


def paused():
    intent, claim, detail = fractional()
    detail["order_id"] = "broker-fixture"
    order_recovery.mark_manual(intent, "CANCEL_EVIDENCE_INVALID")
    scope = outbox.account_symbol_fence_key("identity", "UBER")
    outbox.release_intent_claim("chain", intent["run_id"], "worker")
    outbox.release_chain_dispatch(scope, "worker", claim["claim_token"])
    return outbox.read_intent("chain", intent["run_id"]), detail, scope


def test_reviewed_resume_preserves_fence_halt_and_attempt_witness(monkeypatch):
    intent, detail, scope = paused()
    proposed = resume.plan(intent, detail, "identity")
    monkeypatch.setattr(resume, "datetime", outbox.datetime)
    result = resume.apply_plan(proposed, identity="identity", symbol="UBER",
                               operator="reviewer", read_detail=lambda _: detail)
    assert result["money_fence_retained"] and result["operator_halt_retained"]
    current = outbox.read_intent("chain", intent["run_id"])
    assert current["status"] == "PLACING_UNKNOWN" and current["place_attempted"]
    assert not current["needs_manual_check"]
    fence = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get()
    assert fence["inflight_run_id"] == intent["run_id"] and fence["operator_halt"]["halted"]
    assert current["reconcile_resume"]["operator"] == "reviewer"
    with pytest.raises(ValueError):
        resume.apply_plan(proposed, identity="identity", symbol="UBER",
                          operator="reviewer", read_detail=lambda _: detail)


@pytest.mark.parametrize("change", [{"filled_quantity": "147.13"}, {"status": "SUBMITTED"},
                                   {"total_quantity": "147.13"}, {"symbol": "OTHER"}])
def test_changed_or_nonterminal_broker_proof_never_resumes(monkeypatch, change):
    intent, detail, scope = paused()
    proposed = resume.plan(intent, detail, "identity")
    monkeypatch.setattr(resume, "datetime", outbox.datetime)
    with pytest.raises(ValueError):
        resume.apply_plan(proposed, identity="identity", symbol="UBER", operator="reviewer",
                          read_detail=lambda _: {**detail, **change})
    assert outbox.read_intent("chain", intent["run_id"])["needs_manual_check"]
    assert FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get()["inflight_run_id"]
