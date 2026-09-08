import pytest
from conftest import FAKE_DB
from tools.round3_regression_probe import growing_fill, migration_race


@pytest.fixture(autouse=True)
def clean_db():
    FAKE_DB.store.clear()


@pytest.mark.parametrize("args", [
    {}, {"quantity": 10, "price": 200, "fee": 0.5},
    {"quantity": 20, "price": 201, "fee": 1.2}, {"terminal": False},
])
def test_broker_facts_advance_during_bounded_fifo_checkpoint(args):
    growing_fill(FAKE_DB, **args)


@pytest.mark.parametrize("mode", ["finalize_first", "cancel_writer", "stale_checkpoint"])
def test_migration_rollback_interleavings_preserve_live_pages(mode):
    migration_race(FAKE_DB, mode)


@pytest.mark.parametrize("failure", ["candidate", "missing", "failed", "tampered"])
def test_release_builder_refuses_unverified_pass(tmp_path, failure):
    from tools.build_round3_evidence import REQUIRED, validate
    from tools.candidate_manifest import digest
    log = tmp_path / "actual.log"
    log.write_text("actual validation output", encoding="utf-8")
    validation = {"candidate_hash": "reviewed", "candidate_unchanged": True,
                  "commands": [{"id": name, "exit_code": 0, "log": log.name,
                                "log_sha256": digest(log)} for name in REQUIRED]}
    manifest = {"candidate_hash": "reviewed"}
    assert len(validate(manifest, validation, tmp_path)) == len(REQUIRED)
    if failure == "candidate":
        validation["candidate_hash"] = "other"
    elif failure == "missing":
        validation["commands"].pop()
    elif failure == "failed":
        validation["commands"][0]["exit_code"] = 1
    else:
        log.write_text("changed output", encoding="utf-8")
    with pytest.raises(ValueError):
        validate(manifest, validation, tmp_path)
