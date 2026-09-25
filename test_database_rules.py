"""Static and optional emulator checks for the canonical RTDB rules."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
PUBLIC_READ_PATHS = frozenset({
    "webull_lego_rows",
    "webull_lego_state",
    "webull_lego_order_audit",
    "webull_lego_order_audit_archive",
    "webull_lego_warnings",
})
PRIVATE_PATHS = frozenset({
    "webull_lego_order_outbox",
    "webull_lego_order_outbox_archive",
    "webull_lego_order_dispatch_locks",
    "webull_lego_admin_reconcile_audit",
    "webull_lego_operator_halt_audit",
    "webull_lego_realized",
    "webull_lego_realized_events",
    "webull_lego_realized_lot_pages",
    "webull_lego_broker_cashflow",
    "webull_lego_errors",
    "webull_lego_alert_delivery",
})


def _load(name: str) -> dict:
    with (ROOT / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _allowed(path: str, permission: str) -> bool:
    """Evaluate this ruleset's boolean ancestor grants."""
    node = _load("database.rules.json")["rules"]
    if node.get(permission) is True:
        return True
    for part in (part for part in path.strip("/").split("/") if part):
        node = node.get(part)
        if not isinstance(node, dict):
            return False
        if node.get(permission) is True:
            return True
    return False


def test_firebase_config_points_to_canonical_rules_file():
    assert _load("firebase.json")["database"] == {
        "rules": "database.rules.json",
    }


def test_rules_schema_is_exact_and_fail_closed():
    rules = _load("database.rules.json")["rules"]
    assert rules[".read"] is False
    assert rules[".write"] is False
    assert set(rules) == {
        ".read",
        ".write",
        *PUBLIC_READ_PATHS,
        *PRIVATE_PATHS,
    }
    for path in PUBLIC_READ_PATHS:
        expected = {".read": True, ".write": False}
        if path == "webull_lego_order_audit":
            expected[".indexOn"] = ["placed_at"]
        assert rules[path] == expected
    for path in PRIVATE_PATHS:
        expected = {".read": False, ".write": False}
        if path == "webull_lego_order_outbox":
            expected["$chain"] = {".indexOn": [
                "created_at", "actionable_sort", "audit_pending"]}
        assert rules[path] == expected


@pytest.mark.parametrize(
    "path",
    sorted(PUBLIC_READ_PATHS | {
        f"{path}/record/nested" for path in PUBLIC_READ_PATHS
    }),
)
def test_public_read_matrix(path):
    assert _allowed(path, ".read") is True


@pytest.mark.parametrize(
    "path",
    sorted({
        "",
        "private",
        "private/nested",
        "unmatched",
        "unmatched/nested",
        *PRIVATE_PATHS,
        *(f"{path}/record/nested" for path in PRIVATE_PATHS),
    }),
)
def test_private_unmatched_and_root_reads_are_denied(path):
    assert _allowed(path, ".read") is False


@pytest.mark.parametrize(
    "path",
    sorted({
        "",
        "private",
        "private/nested",
        "unmatched",
        *PUBLIC_READ_PATHS,
        *PRIVATE_PATHS,
        *(f"{path}/record/nested"
          for path in PUBLIC_READ_PATHS | PRIVATE_PATHS),
    }),
)
def test_all_client_writes_are_denied(path):
    assert _allowed(path, ".write") is False


def _emulator_namespace() -> str:
    """The instance the emulator actually loaded database.rules.json into.

    The default Realtime Database instance of project X is named
    'X-default-rtdb', and that is the only namespace firebase.json's rules
    apply to. Asking for any other namespace does not fail — the emulator
    creates it on demand with wide-open default rules — so a wrong name here
    turns the whole matrix below into a test of nothing.
    """
    project = os.environ.get("GCLOUD_PROJECT") or "demo-lego-firebase"
    return f"{project}-default-rtdb"


def _emulator_request(path: str, method: str = "GET") -> int:
    host = os.environ["FIREBASE_DATABASE_EMULATOR_HOST"]
    url = f"http://{host}/{path.strip('/')}.json?ns={_emulator_namespace()}"
    request = Request(
        url,
        method=method,
        data=b"{}" if method != "GET" else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


@pytest.mark.skipif(
    "FIREBASE_DATABASE_EMULATOR_HOST" not in os.environ,
    reason="set FIREBASE_DATABASE_EMULATOR_HOST to run the real rules matrix",
)
def test_emulator_enforces_anonymous_read_write_matrix():
    # Canary first: under the shipped rules the root denies anonymous reads. If
    # this answers 200 the request reached a namespace the rules were never
    # loaded into, and every allow-assertion below would pass for that reason
    # rather than because the ruleset says so.
    assert _emulator_request("") in {401, 403}, (
        f"namespace {_emulator_namespace()!r} is running default-open rules, "
        "not database.rules.json")
    for path in PUBLIC_READ_PATHS:
        assert _emulator_request(path) == 200
        assert _emulator_request(f"{path}/record") == 200
    for path in PRIVATE_PATHS | {"unmatched", "private"}:
        assert _emulator_request(path) in {401, 403}
        assert _emulator_request(f"{path}/record") in {401, 403}
    for path in PUBLIC_READ_PATHS | PRIVATE_PATHS | {"unmatched", "private"}:
        assert _emulator_request(path, method="PUT") in {401, 403}


@pytest.mark.skipif("FIREBASE_DATABASE_EMULATOR_HOST" not in os.environ,
                    reason="real RTDB emulator required for concurrent money-path probes")
@pytest.mark.parametrize("script", ["emulator_race_probe.py", "emulator_tick_race_probe.py",
                                    "emulator_funding_probe.py", "emulator_broker_circuit_probe.py",
                                    "emulator_execution_limits_probe.py",
                                    "emulator_operator_halt_probe.py"])
def test_emulator_concurrent_dispatch_and_tick(script):
    host = os.environ["FIREBASE_DATABASE_EMULATOR_HOST"]
    assert host.startswith(("127.0.0.1:", "localhost:")), "local emulator only"
    result = subprocess.run([sys.executable, str(ROOT / "tools" / script)], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"status": "PASS"' in result.stdout
