import copy
import json
import sys

import pytest

from tools.readiness_audit import build_report, main


def evidence(tmp_path):
    root = {"webull_lego_order_outbox": {"chain": {"run": {
        "status": "NOT_PLACED", "audit_revision": 1, "place_attempted": False}}}}
    root["webull_lego_order_audit"] = copy.deepcopy(root["webull_lego_order_outbox"]["chain"])
    event = {"event": "lego_tick_completed", "correlation_id": "correlation",
             "business_status": "ROW_COMMITTED", "revision": "rev", "candidate_hash": "candidate",
             "timestamp": "2026-09-23T00:00:00Z", "http_status": 200, "duration_ms": 20}
    resource = {"type": "cloud_run_revision", "labels": {
        "project_id": "project", "location": "region", "service_name": "lego-tick",
        "revision_name": "rev"}}
    logs = [{"jsonPayload": event, "trace": "trace", "spanId": "tick-span",
             "timestamp": "2026-09-23T00:00:01.010Z", "resource": copy.deepcopy(resource)},
            {"httpRequest": {"status": 200, "latency": "1s"}, "trace": "trace",
             "spanId": "request-span", "timestamp": "2026-09-23T00:00:00Z",
             "resource": copy.deepcopy(resource)}]
    return root, logs


def report(tmp_path, root, logs):
    export, path = tmp_path / "export.json", tmp_path / "logs.json"
    export.write_text(json.dumps(root), encoding="utf-8")
    path.write_text(json.dumps(logs), encoding="utf-8")
    return build_report(export, path, "candidate", "rev")


def checks(result):
    return {x["criterion"]: x["status"] for x in result["checks"]}


def test_complete_local_evidence_still_does_not_certify_broker(tmp_path):
    result = report(tmp_path, *evidence(tmp_path))
    assert checks(result)["snapshot_integrity"] == "PASS"
    assert checks(result)["request_tick_pairing"] == "PASS"
    pairing = next(c["detail"] for c in result["checks"]
                   if c["criterion"] == "request_tick_pairing")
    assert pairing == {"requests": 1, "ticks": 1, "matched": 1, "issues": {}}
    assert result["real_money_ready"] is False and result["status"] == "BLOCKED"
    assert "run" not in json.dumps(result["evidence"])


def test_equal_counts_are_not_one_to_one_trace_proof(tmp_path):
    root, logs = evidence(tmp_path)
    logs[0].pop("trace")
    result = report(tmp_path, root, logs)
    assert checks(result)["request_tick_pairing"] == "FAIL"


@pytest.mark.parametrize("change,issue", [
    (lambda logs: logs[1].update(trace="other-trace"), "ticks_without_request"),
    (lambda logs: logs.append(copy.deepcopy(logs[1])), "duplicate_request_traces"),
    (lambda logs: logs[1]["resource"]["labels"].update(service_name="other-service"),
     "service_or_revision_mismatch"),
    (lambda logs: logs[0]["resource"]["labels"].update(revision_name="other-revision"),
     "service_or_revision_mismatch"),
    (lambda logs: logs[0].update(timestamp="2026-09-23T00:00:04Z"),
     "tick_outside_request_window"),
    (lambda logs: logs[1]["httpRequest"].pop("latency"), "request_window_missing"),
    (lambda logs: logs[1]["httpRequest"].update(status=503), "http_status_mismatch"),
])
def test_request_tick_pairing_rejects_ambiguous_or_inconsistent_evidence(
        tmp_path, change, issue):
    root, logs = evidence(tmp_path)
    change(logs)
    result = report(tmp_path, root, logs)
    pairing = next(c for c in result["checks"] if c["criterion"] == "request_tick_pairing")
    assert pairing["status"] == "FAIL"
    assert pairing["detail"]["issues"][issue]


def test_stale_mirror_duplicate_tick_candidate_and_live_fence_fail(tmp_path):
    root, logs = evidence(tmp_path)
    root["webull_lego_order_audit"]["run"]["audit_revision"] = 0
    root["webull_lego_order_dispatch_locks"] = {"scope": {"inflight_run_id": "run"}}
    logs[0]["jsonPayload"]["candidate_hash"] = "other"
    logs.append(copy.deepcopy(logs[0]))
    result = checks(report(tmp_path, root, logs))
    for key in ("snapshot_integrity", "request_tick_pairing", "money_fences_resolved",
                "unique_tick_correlations", "candidate_revision_binding"):
        assert result[key] == "FAIL"


def test_missing_or_malformed_evidence_never_passes(tmp_path):
    result = report(tmp_path, {}, [])
    assert all(x["status"] != "PASS" for x in result["checks"] if x["criterion"] != "money_fences_resolved")
    root, logs = evidence(tmp_path)
    root["webull_lego_order_outbox"] = []
    assert checks(report(tmp_path, root, logs))["snapshot_integrity"] == "FAIL"


def test_positive_fill_audit_flags_submitted_quantity_and_identity_gaps(tmp_path):
    root, logs = evidence(tmp_path)
    intent = root["webull_lego_order_outbox"]["chain"]["run"]
    intent.update(status="FILLED", place_attempted=True, side="BUY", symbol="TSLA",
                  quantity="0.31721", filled_quantity="0.320000",
                  order_payload=[{"client_order_id": "run", "side": "BUY",
                                  "symbol": "TSLA", "quantity": "0.31721"}])
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    result = report(tmp_path, root, logs)
    detail = next(c["detail"] for c in result["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["fill_exceeds_submitted_quantity"] == 1
    assert "intent_payload_quantity_mismatch" not in detail["issues"]

    intent["order_payload"][0].update(quantity="0.3", symbol="AAPL")
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["intent_payload_quantity_mismatch"] == 1
    assert detail["issues"]["submitted_order_identity_mismatch"] == 1


def test_attempted_terminal_missing_quantity_and_reject_reason_fail_audit(tmp_path):
    root, logs = evidence(tmp_path)
    intent = root["webull_lego_order_outbox"]["chain"]["run"]
    intent.update(status="FAILED", place_attempted=True,
                  broker_reason_missing=True)
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["invalid_fill_quantity"] == 1
    assert detail["issues"]["broker_rejection_reason_missing"] == 1

    intent["filled_quantity"] = "0"
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["broker_rejection_reason_missing"] == 1


def test_canceled_alias_is_terminal_in_snapshot_audit(tmp_path):
    root, logs = evidence(tmp_path)
    intent = root["webull_lego_order_outbox"]["chain"]["run"]
    intent.update(status="CANCELED", place_attempted=True, filled_quantity="0")
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["statuses"] == {"CANCELLED": 1}
    assert "unresolved_execution" not in detail["issues"]


def test_cli_redacts_invalid_raw_content_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bad.json"
    path.write_text("secret-account-do-not-print", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["readiness_audit", "--export", str(path), "--logs", str(path),
                                     "--candidate", "candidate", "--revision", "rev"])
    assert main() == 1
    output = capsys.readouterr().out
    assert "secret-account" not in output
    assert json.loads(output)["status"] == "BLOCKED"
