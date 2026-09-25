import copy
import json
import sys

import pytest

from tools.readiness_audit import build_report, main


def evidence(tmp_path):
    root = {"webull_lego_order_outbox": {"chain": {"run": {
        "status": "NOT_PLACED", "audit_revision": 1, "place_attempted": False,
        "row_status": "READY_BUY", "chain_key": "chain", "run_id": "run",
        "side": "BUY", "symbol": "TSLA", "quantity": "1"}}}}
    root["webull_lego_order_audit"] = copy.deepcopy(root["webull_lego_order_outbox"]["chain"])
    root["webull_lego_rows"] = {"run": {
        "run_id": "run", "chain_key": "chain", "committed": True,
        "สถานะ": "READY_BUY", "ฝั่ง": "BUY", "สินทรัพย์": "TSLA",
        "จำนวนสั่ง (หุ้น)": "1", "เวลา (UTC)": "2026-09-23T00:00:00Z",
        "DNA step": 0, "market_slot_id": "slot",
        "cashflow_status": "PENDING_EXECUTION",
        "ΔAₙ ต่อสเต็ป (USD)": 0, "ΔAₙ เงินจริง (USD)": 0,
        "Aₙ สะสม (USD)": 0, "Eₙ ส่วนเกินสะสม (USD)": 0}}
    event = {"event": "lego_tick_completed", "correlation_id": "correlation",
             "business_status": "ROW_COMMITTED", "revision": "rev", "candidate_hash": "candidate",
             "timestamp": "2026-09-23T00:00:00Z", "http_status": 200, "duration_ms": 20,
             "decision": {"run_id": "run", "status": "READY_BUY", "step": 0,
                          "market_slot_id": "slot", "committed": True}}
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
    assert checks(result)["committed_row_log_lineage"] == "PASS"
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


def test_operator_halt_and_unmirrored_transition_block_release(tmp_path):
    root, logs = evidence(tmp_path)
    root["webull_lego_order_dispatch_locks"] = {"scope": {
        "operator_halt": {"halted": True, "audit_pending_event": {"event_id": "event"}}}}
    result = report(tmp_path, root, logs)
    money = next(c for c in result["checks"] if c["criterion"] == "money_fences_resolved")
    assert money["status"] == "FAIL"
    assert money["detail"]["operator_halts"] == 1
    assert money["detail"]["operator_halt_audits_pending"] == 1


def test_cleared_operator_halt_requires_immutable_history(tmp_path):
    root, logs = evidence(tmp_path)
    halt = {"halted": False, "halt_id": "h1", "last_audit_event_id": "clear"}
    root["webull_lego_order_dispatch_locks"] = {"scope": {"operator_halt": halt}}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["operator_halt_audit_history_missing"] == 1

    root["webull_lego_operator_halt_audit"] = {"scope": {
        "set": {"scope": "scope", "halt_id": "h1", "action": "HALT"},
        "clear": {"scope": "scope", "halt_id": "h1", "action": "CLEAR"}}}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert "operator_halt_audit_history_missing" not in detail["issues"]
    assert "operator_halt_last_audit_witness_missing" not in detail["issues"]


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
                  order_contract_anomaly="fill_exceeds_submitted_quantity",
                  order_payload=[{"client_order_id": "run", "side": "BUY",
                                  "symbol": "TSLA", "quantity": "0.31721"}])
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    result = report(tmp_path, root, logs)
    detail = next(c["detail"] for c in result["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["fill_exceeds_submitted_quantity"] == 1
    assert detail["issues"]["order_contract_anomaly"] == 1
    assert "intent_payload_quantity_mismatch" not in detail["issues"]

    root["webull_lego_order_audit"]["run"].pop("order_contract_anomaly")
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["mirror_missing_stale_or_mismatched"] == 1

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


def test_intent_requires_committed_matching_decision_row(tmp_path):
    root, logs = evidence(tmp_path)
    root["webull_lego_rows"].clear()
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["committed_decision_row_missing"] == 1

    root, logs = evidence(tmp_path)
    root["webull_lego_rows"]["run"]["จำนวนสั่ง (หุ้น)"] = "2"
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["intent_decision_row_mismatch"] == 1


def test_archived_order_and_audit_still_count_as_one_history(tmp_path):
    root, logs = evidence(tmp_path)
    root["webull_lego_order_outbox_archive"] = root.pop("webull_lego_order_outbox")
    root["webull_lego_order_audit_archive"] = root.pop("webull_lego_order_audit")
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"] == {}
    assert detail["intents"] == 1 and detail["mirrors_matched"] == 1

    # A crash between copy and hot-path delete may briefly leave identical
    # records in both locations. The audit must count one logical order.
    root["webull_lego_order_outbox"] = copy.deepcopy(
        root["webull_lego_order_outbox_archive"])
    root["webull_lego_order_audit"] = copy.deepcopy(
        root["webull_lego_order_audit_archive"])
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"] == {}
    assert detail["intents"] == 1

    root["webull_lego_order_outbox"]["chain"]["run"]["status"] = "FILLED"
    root["webull_lego_order_audit"]["run"]["status"] = "FILLED"
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["live_archive_outbox_conflict"] == 1
    assert detail["issues"]["live_archive_order_audit_conflict"] == 1


def test_orphaned_money_witnesses_fail_snapshot_integrity(tmp_path):
    root, logs = evidence(tmp_path)
    orphan = copy.deepcopy(root["webull_lego_rows"]["run"])
    orphan.update(run_id="orphan", cashflow_status="FINALIZED")
    root["webull_lego_rows"]["orphan"] = orphan
    root["webull_lego_order_audit"]["orphan"] = {"status": "FILLED"}
    root["webull_lego_broker_cashflow"] = {"chain": {"events": {"orphan": {}}}}
    root["webull_lego_state"] = {"chain": {"execution_cashflow": {
        "finalized_runs": {"orphan": {}}}}}
    root["webull_lego_realized_events"] = {"chain": {"orphan": {}}}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    for issue in ("finalized_row_without_intent", "order_audit_without_intent",
                  "broker_cashflow_without_intent", "model_finalization_without_intent",
                  "realized_fill_without_intent"):
        assert detail["issues"][issue] == 1


def test_committed_row_requires_matching_tick_in_log_window(tmp_path):
    root, logs = evidence(tmp_path)
    logs[0]["jsonPayload"]["decision"]["run_id"] = "other"
    result = report(tmp_path, root, logs)
    lineage = next(c for c in result["checks"]
                   if c["criterion"] == "committed_row_log_lineage")
    assert lineage["status"] == "FAIL"
    assert lineage["detail"]["issues"]["commit_event_row_missing_or_mismatched"] == 1
    assert lineage["detail"]["issues"]["scoped_row_commit_event_missing"] == 1


def test_positive_fill_witness_values_must_agree_across_ledgers(tmp_path):
    root, logs = evidence(tmp_path)
    intent = root["webull_lego_order_outbox"]["chain"]["run"]
    intent.update(status="FILLED", place_attempted=True,
                  filled_quantity="1", filled_price="10", filled_fee="0.25",
                  broker_fee_status="KNOWN", cashflow_finalized=True, realized=True,
                  order_payload=[{"client_order_id": "run", "symbol": "TSLA",
                                  "side": "BUY", "quantity": "1"}])
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    row = root["webull_lego_rows"]["run"]
    row.update(cashflow_status="FINALIZED", execution_quantity="1",
               execution_price="10", **{
                   "ΔAₙ ต่อสเต็ป (USD)": "1",
                   "Aₙ สะสม (USD)": "2",
                   "Eₙ ส่วนเกินสะสม (USD)": "3"})
    root["webull_lego_broker_cashflow"] = {"chain": {"events": {"run": {
        "cumulative_quantity": "1", "cumulative_notional": "10",
        "actual_fees": "0.25", "cash_cumulative": "-10.25", "side": "BUY"}}}}
    root["webull_lego_state"] = {"chain": {"execution_cashflow": {
        "finalized_runs": {"run": {"filled_quantity": "1", "filled_price": "10",
                                   "delta_actual": "1", "actual_cumulative": "2",
                                   "excess": "3"}}}}}
    root["webull_lego_realized"] = {"chain": {"applied_fills": {"run": {
        "quantity": "1", "average_price": "10", "fee": "0.25", "side": "BUY"}}}}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"] == {}

    root["webull_lego_realized_events"] = {"chain": {"run":
        root["webull_lego_realized"]["chain"]["applied_fills"].pop("run")}}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"] == {}

    root["webull_lego_realized"]["chain"]["applied_fills"]["run"] = {
        **root["webull_lego_realized_events"]["chain"]["run"], "fee": "0.30"}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["live_archive_realized_conflict"] == 1
    root["webull_lego_realized"]["chain"]["applied_fills"].pop("run")

    root["webull_lego_realized_events"]["chain"]["run"]["average_price"] = 10.000000001
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"] == {}

    root["webull_lego_realized_events"]["chain"]["run"]["quantity"] = "0.9"
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["model_or_realized_witness_mismatch"] == 1


def test_snapshot_flags_money_columns_even_when_fill_witnesses_exist(tmp_path):
    root, logs = evidence(tmp_path)
    intent = root["webull_lego_order_outbox"]["chain"]["run"]
    intent.update(status="FILLED", place_attempted=True,
                  filled_quantity="1", filled_price="10", filled_fee="0.25",
                  broker_fee_status="KNOWN", cashflow_finalized=True, realized=True,
                  order_payload=[{"client_order_id": "run", "symbol": "TSLA",
                                  "side": "BUY", "quantity": "1"}])
    root["webull_lego_order_audit"]["run"] = copy.deepcopy(intent)
    row = root["webull_lego_rows"]["run"]
    row.update(cashflow_status="FINALIZED", execution_quantity="1",
               execution_price="10", **{
                   "ΔAₙ ต่อสเต็ป (USD)": 9,
                   "Aₙ สะสม (USD)": 8,
                   "Eₙ ส่วนเกินสะสม (USD)": 7,
               })
    root["webull_lego_broker_cashflow"] = {"chain": {"events": {"run": {
        "cumulative_quantity": "1", "cumulative_notional": "10",
        "actual_fees": "0.25", "cash_cumulative": "-10.25", "side": "BUY"}}}}
    root["webull_lego_state"] = {"chain": {"execution_cashflow": {
        "finalized_runs": {"run": {"filled_quantity": "1", "filled_price": "10",
                                   "delta_actual": "1", "actual_cumulative": "2",
                                   "excess": "3"}}}}}
    root["webull_lego_realized"] = {"chain": {"applied_fills": {"run": {
        "quantity": "1", "average_price": "10", "fee": "0.25", "side": "BUY"}}}}
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["model_or_realized_witness_mismatch"] == 1


def test_snapshot_flags_unexecuted_row_that_moves_model_money(tmp_path):
    root, logs = evidence(tmp_path)
    row = root["webull_lego_rows"]["run"]
    row.update(cashflow_status="NO_ACTION", **{
        "ΔAₙ ต่อสเต็ป (USD)": 1.0,
        "ΔAₙ เงินจริง (USD)": 0.0,
        "Aₙ สะสม (USD)": 1.0,
        "Eₙ ส่วนเกินสะสม (USD)": 1.0,
    })
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["unexecuted_row_delta_nonzero"] == 1

    row["ΔAₙ ต่อสเต็ป (USD)"] = 0
    row["execution_quantity"] = "1"
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["unexecuted_row_has_execution_witness"] == 1

    row["execution_quantity"] = None
    row["cashflow_status"] = "UNKNOWN"
    detail = next(c["detail"] for c in report(tmp_path, root, logs)["checks"]
                  if c["criterion"] == "snapshot_integrity")
    assert detail["issues"]["row_cashflow_status_unknown"] == 1


def test_cli_redacts_invalid_raw_content_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bad.json"
    path.write_text("secret-account-do-not-print", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["readiness_audit", "--export", str(path), "--logs", str(path),
                                     "--candidate", "candidate", "--revision", "rev"])
    assert main() == 1
    output = capsys.readouterr().out
    assert "secret-account" not in output
    assert json.loads(output)["status"] == "BLOCKED"
