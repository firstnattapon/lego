"""Acceptance must not manufacture PASS or reuse another candidate's results."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from tools.build_release_evidence import build, REQUIRED
from tools.candidate_manifest import digest


@pytest.fixture
def captured(tmp_path):
    log = tmp_path / "pytest.log"
    log.write_text("actual command output\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    record = {"command": ["python", "-m", "pytest"], "log": log.name,
              "log_sha256": digest(log), "exit_code": 0,
              "started_utc": (now - timedelta(seconds=1)).isoformat(),
              "finished_utc": now.isoformat()}
    manifest = {"candidate_hash": "a" * 64, "dependency_lock_hash": "b" * 64,
                "scope": "backend-only"}
    validation = {"candidate_hash": manifest["candidate_hash"], "candidate_unchanged": True,
                  "commands": [{**record, "id": key} for key in REQUIRED]}
    return validation, manifest, tmp_path


def test_all_local_checks_never_certify_live_acceptance(captured):
    report = build(*captured)
    assert report["local_status"] == "PASS"
    assert report["summary"]["pass"] == len(REQUIRED)
    assert report["release_state"] == "NO_GO" and not report["real_money_ready"]
    assert all(gate["status"] == "BLOCKED" for gate in report["external_gates"])


@pytest.mark.parametrize("field,value", [("candidate_hash", "c" * 64),
                                         ("candidate_unchanged", False)])
def test_other_or_changed_candidate_rejected(captured, field, value):
    validation, manifest, directory = captured
    validation[field] = value
    with pytest.raises(ValueError, match="candidate"):
        build(validation, manifest, directory)


def test_missing_command_is_blocked_and_reader_scope_is_required(captured):
    validation, manifest, directory = captured
    validation["commands"] = []
    manifest["scope"] = "backend-and-reader"
    report = build(validation, manifest, directory)
    assert report["local_status"] == "BLOCKED"
    assert report["summary"]["blocked"] == len(REQUIRED) + 1


@pytest.mark.parametrize("fault", ["hash", "traversal", "absolute", "exit", "boolean",
                                   "time", "naive", "future", "missing_field"])
def test_invalid_evidence_fails(captured, fault):
    validation, manifest, directory = captured
    record = validation["commands"][0]
    if fault == "hash": record["log_sha256"] = "0" * 64
    if fault == "traversal": record["log"] = "../pytest.log"
    if fault == "absolute": record["log"] = str(directory / "pytest.log")
    if fault == "exit": record["exit_code"] = 1
    if fault == "boolean": record["exit_code"] = False
    if fault == "time": record["started_utc"] = "2099-01-01T00:00:00Z"
    if fault == "naive": record["finished_utc"] = "2026-01-01T00:00:00"
    if fault == "future": record["finished_utc"] = "2099-01-01T00:00:00Z"
    if fault == "missing_field": del record["exit_code"]
    assert build(validation, manifest, directory)["local_status"] == "FAIL"


def test_duplicate_command_cannot_shadow_failure(captured):
    validation, manifest, directory = captured
    bad = copy.deepcopy(validation["commands"][0])
    bad["exit_code"] = 1
    validation["commands"].insert(0, bad)
    with pytest.raises(ValueError, match="duplicate"):
        build(validation, manifest, directory)


@pytest.mark.parametrize("empty", [True, False])
def test_absent_or_empty_raw_log_cannot_pass(captured, empty):
    validation, manifest, directory = captured
    log = directory / "pytest.log"
    if empty:
        log.write_text("", encoding="utf-8")
        for record in validation["commands"]:
            record["log_sha256"] = digest(log)
    else:
        log.unlink()
    assert build(validation, manifest, directory)["local_status"] == "BLOCKED"


def test_cli_preserves_existing_evidence(captured, monkeypatch):
    import json
    import sys
    from tools import build_release_evidence as cli
    validation, manifest, directory = captured
    source, target = directory / "validation.json", directory / "release.json"
    source.write_text(json.dumps(validation), encoding="utf-8")
    target.write_text("historical evidence", encoding="utf-8")
    monkeypatch.setattr(cli, "build_manifest", lambda: manifest)
    monkeypatch.setattr(sys, "argv", ["evidence", "--validation", str(source), "--output", str(target)])
    with pytest.raises(FileExistsError):
        cli.main()
    assert target.read_text(encoding="utf-8") == "historical evidence"
