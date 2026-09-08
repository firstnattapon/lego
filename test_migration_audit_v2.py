import json

from tools.migration_audit import audit_export


def test_migration_audit_blocks_attempted_or_unknown_order(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text(json.dumps({"webull_lego_outbox": {"chain": {
        "safe": {"status": "UNSENT_ABORTED", "place_attempted": False},
        "unsafe": {"status": "PLACING_UNKNOWN", "place_attempted": True},
    }}}), encoding="utf-8")
    result = audit_export(export)
    assert result["outbox_intents"] == 2
    assert result["cutover_safe"] is False
    assert result["attempted_or_unresolved"] == [{
        "chain_key": "chain", "run_id": "unsafe", "status": "PLACING_UNKNOWN",
    }]


def test_migration_audit_hashes_safe_export(tmp_path):
    export = tmp_path / "backup.json"
    export.write_text('{"webull_lego_outbox":{}}', encoding="utf-8")
    result = audit_export(export)
    assert result["cutover_safe"] is True
    assert len(result["export_sha256"]) == 64

