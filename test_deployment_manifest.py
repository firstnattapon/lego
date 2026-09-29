from copy import deepcopy
import json
import pytest
from tools.deployment_manifest import build


def service():
    env = {"LEGO_GIT_COMMIT": "a" * 40, "LEGO_CANDIDATE_HASH": "b" * 64,
           "WEBULL_ENV": "PROD", "LEGO_SYMBOL": "UBER", "LEGO_MODE": "observe",
           "LEGO_ACTIVE": "false", "LEGO_RELEASE_AUTHORIZATION": "private-binding"}
    return {"spec": {"template": {"metadata": {"name": "revision-1"}, "spec": {"containers": [{
        "image": "registry/runtime@sha256:" + "c" * 64,
        "env": [{"name": key, "value": value} for key, value in env.items()] + [{
            "name": "WEBULL_ACCOUNT_ID", "valueFrom": {"secretKeyRef": {"name": "private-account", "key": "1"}}}]}]}}},
        "status": {"latestReadyRevisionName": "revision-1", "traffic": [{"revisionName": "revision-1", "percent": 100}]}}


def test_receipt_binds_revision_and_redacts_release_account():
    receipt = build(service(), git_commit="a" * 40, candidate="b" * 64)
    assert receipt["deployment_revision"] == "revision-1"
    assert "private-binding" not in json.dumps(receipt)
    assert "private-account" not in json.dumps(receipt)
    assert len(receipt["receipt_sha256"]) == 64


@pytest.mark.parametrize("failure", ["commit", "candidate", "image", "traffic", "revision"])
def test_receipt_refuses_incomplete_provenance(failure):
    captured = deepcopy(service())
    container = captured["spec"]["template"]["spec"]["containers"][0]
    if failure == "commit": container["env"][0]["value"] = "d" * 40
    if failure == "candidate": container["env"][1]["value"] = "d" * 64
    if failure == "image": container["image"] = "registry/runtime:latest"
    if failure == "traffic": captured["status"]["traffic"][0]["percent"] = 50
    if failure == "revision": captured["status"]["latestReadyRevisionName"] = "other"
    with pytest.raises(ValueError):
        build(captured, git_commit="a" * 40, candidate="b" * 64)
