"""Provenance and integrity checks for the metadata-only Webull wheel patch."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WHEEL = (
    ROOT / "vendor"
    / "webull_openapi_python_sdk-2.0.15-1lego-py3-none-any.whl"
)
DIST_INFO = "webull_openapi_python_sdk-2.0.15.dist-info"
PATCHED_SHA256 = (
    "73d252bc82ebdc5a2c53bc94122994ecfb29d44e76f4af33defe27bbdccca1c6"
)
ORIGINAL_RUNTIME_AGGREGATE = (
    "0c6be72befa78586ceb51674b99050ac2edf250efee72b766559a1f620a9079d"
)


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def test_vendored_wheel_hash_and_runtime_code_provenance():
    assert hashlib.sha256(WHEEL.read_bytes()).hexdigest() == PATCHED_SHA256
    with zipfile.ZipFile(WHEEL) as wheel:
        runtime_names = sorted(
            name for name in wheel.namelist() if name.startswith("webull/"))
        aggregate = b"".join(
            (
                name + "\0" + hashlib.sha256(wheel.read(name)).hexdigest() + "\n"
            ).encode()
            for name in runtime_names
        )
    assert len(runtime_names) == 280
    assert hashlib.sha256(aggregate).hexdigest() == ORIGINAL_RUNTIME_AGGREGATE


def test_vendored_wheel_has_a_complete_valid_record():
    with zipfile.ZipFile(WHEEL) as wheel:
        record_name = f"{DIST_INFO}/RECORD"
        rows = {
            row[0]: row[1:]
            for row in csv.reader(
                io.StringIO(wheel.read(record_name).decode("utf-8")))
        }
        assert set(rows) == set(wheel.namelist())
        for name in wheel.namelist():
            if name == record_name:
                assert rows[name] == ["", ""]
                continue
            data = wheel.read(name)
            assert rows[name] == [_record_digest(data), str(len(data))]


def test_vendored_wheel_only_allows_audited_cryptography_floor():
    with zipfile.ZipFile(WHEEL) as wheel:
        metadata = wheel.read(f"{DIST_INFO}/METADATA").decode("utf-8")
        assert "Version: 2.0.15" in metadata
        assert (
            'cryptography<55,>=50.0.0; python_version >= "3.12" '
            'and python_version < "3.14"'
        ) in metadata
        assert (
            'cryptography<55,>=48.0.1; python_version >= "3.14"'
        ) in metadata
        assert (
            f"{DIST_INFO}/licenses/LICENSE" in wheel.namelist()
            and f"{DIST_INFO}/licenses/NOTICE" in wheel.namelist()
        )
