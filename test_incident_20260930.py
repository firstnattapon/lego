"""Regression for tagged deployment receipts and hidden broker blockers."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from conftest import FAKE_DB
import execution_service as execution
import lego_outbox as outbox
import main
import observability
import open_order_blocker as blocker
import ops
import operational_health
from test_deployment_manifest import service
from tools.deployment_manifest import build
from tools.monitoring_config import build as monitoring


@pytest.fixture(autouse=True)
def isolated():
    FAKE_DB.store.clear()
    yield
    FAKE_DB.store.clear()


def cloud_function_capture():
    captured = service()
    captured["metadata"] = {"name": "lego-tick-uat"}
    template = captured["spec"]["template"]
    revision = {"metadata": {"name": "revision-1", "labels": {
        "serving.knative.dev/service": "lego-tick-uat"}},
        "spec": copy.deepcopy(template["spec"]), "status": {
            "imageDigest": template["spec"]["containers"][0]["image"],
            "conditions": [{"type": "Ready", "status": "True"}]}}
    template["spec"]["containers"][0]["image"] = "registry/runtime:version_1"
    return captured, revision


def test_tagged_service_receipt_uses_matching_ready_revision():
    captured, revision = cloud_function_capture()
    receipt = build(captured, git_commit="a" * 40, candidate="b" * 64, revision=revision)
    assert receipt["container_image"] == revision["status"]["imageDigest"]
    assert "private-account" not in json.dumps(receipt)


def test_unnamed_service_template_requires_matching_revision_capture():
    captured, revision = cloud_function_capture()
    del captured["spec"]["template"]["metadata"]["name"]
    assert build(captured, git_commit="a" * 40, candidate="b" * 64,
                 revision=revision)["deployment_revision"] == "revision-1"
    captured["spec"]["template"]["spec"]["containers"][0]["image"] = revision["status"]["imageDigest"]
    with pytest.raises(ValueError, match="binding incomplete"):
        build(captured, git_commit="a" * 40, candidate="b" * 64)


@pytest.mark.parametrize("fault", ["missing", "revision", "service", "ready", "digest", "repository", "env", "immutable"])
def test_another_revision_or_changed_settings_cannot_certify_tag(fault):
    captured, revision = cloud_function_capture()
    if fault == "missing": revision = None
    if fault == "revision": revision["metadata"]["name"] = "other"
    if fault == "service": revision["metadata"]["labels"]["serving.knative.dev/service"] = "other"
    if fault == "ready": revision["status"]["conditions"][0]["status"] = "False"
    if fault == "digest": revision["status"]["imageDigest"] = "registry/runtime@sha256:" + "d" * 64
    if fault == "repository": captured["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/other:tag"
    if fault == "env": revision["spec"]["containers"][0]["env"][0]["value"] = "d" * 40
    if fault == "immutable": captured["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/runtime@sha256:" + "d" * 64
    with pytest.raises(ValueError):
        build(captured, git_commit="a" * 40, candidate="b" * 64, revision=revision)


def claim():
    scope = outbox.account_symbol_fence_key("identity", "UBER")
    return scope, outbox.claim_chain_dispatch(scope, "worker", lease_seconds=120)


def orders():
    return [{"client_order_id": "original-order", "symbol": "UBER", "status": "SUBMITTED",
             "access_token": "secret-fixture", "account_id": "private-account"}]


def test_blocked_dispatch_records_identity_without_preview_place_or_cancel(monkeypatch):
    scope, lease = claim()
    intent = outbox.put_intent("chain", "new-run", {"status": "PENDING_DISPATCH",
        "runtime_identity_fingerprint": "identity", "symbol": "UBER",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    intent = outbox.claim_intent("chain", "new-run", "worker", lease_seconds=120)
    monkeypatch.setattr(execution, "read_committed_row", lambda *_: {"committed": True})
    monkeypatch.setattr(execution, "fetch_open_orders", lambda *_: orders())
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_: pytest.fail("blocked order requested snapshot"))
    monkeypatch.setattr(execution, "place_market_order", lambda *_: pytest.fail("blocked order placed"))
    result = execution._dispatch_or_reconcile_one(None, None, SimpleNamespace(symbol="UBER"), intent, lease)
    assert result["status"] == "SUPPRESSED_ACTIVE_ORDER" and result["open_order_count"] == 1
    saved = outbox.read_intent("chain", "new-run")["broker_open_order_blocker"]
    assert saved["orders"][0]["client_order_id"] == "original-order"
    assert "secret-fixture" not in json.dumps(saved)
    assert FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get()["broker_open_order_blocker"]
    mirror = FAKE_DB.reference("webull_lego_order_audit/new-run").get()
    assert mirror["status"] == "SUPPRESSED_ACTIVE_ORDER"
    assert "broker_open_order_blocker" not in mirror
    assert "original-order" not in json.dumps(mirror)
    assert "original-order" not in json.dumps(result)


def test_idle_worker_preserves_blocker_without_extra_broker_read(monkeypatch):
    scope, lease = claim()
    blocker.record(scope, lease, orders())
    outbox.release_chain_dispatch(scope, "worker", lease["claim_token"])
    monkeypatch.setattr(execution, "chain_key", lambda *_: "chain")
    monkeypatch.setattr(execution, "read_chain_state", lambda *_: {})
    monkeypatch.setattr(execution, "chain_runtime_identity_is_verified", lambda *a, **k: True)
    monkeypatch.setattr(execution, "verify_runtime_identity", lambda *_: None)
    monkeypatch.setattr(execution, "_announce_identity_adoption", lambda *_: None)
    monkeypatch.setattr(execution, "_recover_pending_order_intents", lambda *a, **k: None)
    monkeypatch.setattr(execution, "_repair_pending_audits", lambda *_: None)
    monkeypatch.setattr(execution, "build_clients", lambda: pytest.fail("idle worker queried broker"))
    result = execution._run_order_worker(SimpleNamespace(symbol="UBER"), runtime_identity="identity")
    assert result["open_order_blocked"] and result["processed"] == 0
    assert observability.business_status({"recovery": result, "operational_health": {"release_expiring": True}}, 200) == "OPEN_ORDER_BLOCKED"


def test_stale_worker_cannot_clear_observation_or_inflight_fence():
    scope, lease = claim()
    outbox.fence_chain_dispatch(scope, "inflight", "worker", lease["claim_token"])
    blocker.record(scope, lease, orders())
    with pytest.raises(outbox.StaleIntentClaim):
        blocker.record(scope, {**lease, "claim_token": "stale"}, [])
    blocker.record(scope, lease, [])
    state = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}").get()
    assert state["broker_open_order_blocker"] is None
    assert state["inflight_run_id"] == "inflight"


def test_unowned_lease_cannot_write_blocker():
    scope, lease = claim()
    ref = FAKE_DB.reference(f"{outbox.DISPATCH_LOCK_PATH}/{scope}")
    ref.update({"owner": None, "claim_token": None})
    with pytest.raises(outbox.StaleIntentClaim):
        blocker.record(scope, {}, orders())


def test_blocker_logs_and_monitoring_are_actionable_and_redacted(capsys):
    witness = blocker.public(blocker.describe(orders()))
    body = {"recovery": {"results": [], **witness}, "operational_health": {"release_expiring": True}}
    observability.emit_tick(body, 200)
    text = capsys.readouterr().out
    event = json.loads(text)
    assert event["business_status"] == "OPEN_ORDER_BLOCKED" and event["severity"] == "WARNING"
    assert event["open_order_blockers"][0]["open_order_count"] == 1
    assert "original-order" not in text and "secret-fixture" not in text
    assert "OPEN_ORDER_BLOCKED" in monitoring("lego-tick-uat", "projects/demo/notificationChannels/1")["health-policy"]["conditions"][0]["conditionMatchedLog"]["filter"]


def test_operator_inspection_is_read_only_and_does_not_claim_ownership(monkeypatch):
    runtime = SimpleNamespace(operator=SimpleNamespace(symbol="UBER", principal_usd=10000,
        diff_usd=25, dna_bundle=SimpleNamespace(dna_code="bypass:500")),
        deployment=SimpleNamespace(environment="UAT", account_fingerprint="opaque"))
    monkeypatch.setattr(ops, "load_runtime_config", lambda: runtime)
    monkeypatch.setattr(main, "_init_firebase", lambda: None)
    monkeypatch.setattr(main, "build_clients", lambda: (None, None))
    calls = []
    monkeypatch.setattr(main, "fetch_open_orders", lambda *_: calls.append(1) or orders())
    result = ops.inspect_open_orders_command(None)
    assert result["read_only"] and result["real_money_ready"] is False
    assert result["orders"][0]["ownership"] == "NOT_FOUND_IN_CURRENT_CHAIN"
    assert calls == [1] and not FAKE_DB.store


def test_expired_horizons_are_visible_even_when_market_decision_has_no_remaining(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "900")
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-09-29T13:30:00Z")
    runtime = SimpleNamespace(operator=SimpleNamespace(dna_bundle=SimpleNamespace(
        dna_code="bypass:1", interval_seconds=900, origin_utc="2026-09-29T13:30:00Z")),
        deployment=SimpleNamespace(execution_limits=("1000", "36000", "30", "2026-09-30T15:12:22Z")))
    health = operational_health.report(runtime, {}, {}, now=datetime(2026, 9, 30, 15, 12, 22, tzinfo=timezone.utc))
    assert health["release_expired"] and health["release_seconds_remaining"] == 0
    assert health["dna_exhausted"]
    assert observability.business_status({"operational_health": health}, 200) == "RELEASE_EXPIRED"
    assert observability.business_status({"operational_health": {"dna_exhausted": True}}, 200) == "DNA_EXHAUSTED"
