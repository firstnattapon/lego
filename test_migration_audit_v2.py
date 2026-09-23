import json
import sys

from tools.migration_audit import audit_export, main


def test_migration_audit_blocks_attempted_or_unknown_order(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({"webull_lego_order_outbox": {"chain": {
        "safe": {"status": "UNSENT_ABORTED", "place_attempted": False},
        "unsafe": {"status": "PLACING_UNKNOWN", "place_attempted": True},
    }}}), encoding="utf-8")
    result = audit_export(export)
    assert result["outbox_intents"] == 2
    assert result["cutover_safe"] is False
    assert result["attempted_or_unresolved"] == [{
        "path": "webull_lego_order_outbox", "chain_key": "chain",
        "run_id": "unsafe", "status": "PLACING_UNKNOWN",
    }]


def test_migration_audit_hashes_safe_export(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text('{"webull_lego_order_outbox":{}}', encoding="utf-8")
    result = audit_export(export)
    assert result["cutover_safe"] is True
    assert len(result["export_sha256"]) == 64


def test_migration_audit_fails_closed_on_missing_or_malformed_outbox(tmp_path):
    export = tmp_path / "backup.json"
    for source in ({}, {"webull_lego_order_outbox": None},
                   {"webull_lego_order_outbox": {"chain": {"run": None}}}):
        export.write_text(json.dumps(source), encoding="utf-8")
        result = audit_export(export)
        assert result["cutover_safe"] is False
        assert result["issues"]


def test_migration_audit_checks_legacy_and_current_nodes_and_fences(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({
        "webull_lego_order_outbox": {"chain": {
            "current": {"status": "EXPIRED_UNSENT", "place_attempted": False},
        }},
        "webull_lego_outbox": {"chain": {
            "legacy": {"status": "FILLED", "place_attempted": True},
        }},
        "webull_lego_order_dispatch_locks": {"account_symbol": {
            "inflight_run_id": "legacy",
        }},
    }), encoding="utf-8")
    result = audit_export(export)
    assert result["outbox_intents"] == 2
    assert result["cutover_safe"] is False
    assert result["active_fences"] == ["account_symbol"]
    assert [item["run_id"] for item in result["attempted_or_unresolved"]] == ["legacy"]


def test_migration_audit_blocks_unattempted_pending_and_manual_intents(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({"webull_lego_order_outbox": {"chain": {
        "pending": {"status": "PENDING_DISPATCH", "place_attempted": False},
        "manual": {"status": "RECONCILE_ABANDONED", "place_attempted": False,
                   "needs_manual_check": True},
    }}}), encoding="utf-8")
    result = audit_export(export)
    assert result["cutover_safe"] is False
    assert {item["run_id"] for item in result["attempted_or_unresolved"]} == {
        "pending", "manual",
    }


def test_migration_audit_accepts_terminal_unsent_without_attempt_field(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({"webull_lego_order_outbox": {"chain": {
        "expired": {"status": "EXPIRED_UNSENT"},
        "suppressed": {"status": "SUPPRESSED_STATE_CHANGED"},
    }}}), encoding="utf-8")
    result = audit_export(export)
    assert result["cutover_safe"] is True
    assert result["attempted_or_unresolved"] == []


def test_migration_audit_blocks_broker_marker_on_unsent_status(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({"webull_lego_order_outbox": {"chain": {
        "contradiction": {"status": "EXPIRED_UNSENT", "placed_at": "2026-09-22T14:00:00Z"},
    }}}), encoding="utf-8")
    assert audit_export(export)["cutover_safe"] is False


def test_migration_audit_cli_returns_failure_for_unsafe_export(
        tmp_path, monkeypatch, capsys):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({"webull_lego_order_outbox": {"chain": {
        "unresolved": {"status": "RECONCILE_ABANDONED", "place_attempted": True},
    }}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["migration_audit.py", str(export)])
    assert main() == 1
    assert json.loads(capsys.readouterr().out)["cutover_safe"] is False
