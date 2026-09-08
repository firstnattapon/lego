"""Raise only the vendored Webull wheel's cryptography compatibility floor.

The input is the previously audited 2.0.15-1lego wheel. Runtime package bytes
are copied unchanged; only METADATA and its RECORD entry are regenerated.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import sys
import zipfile
from pathlib import Path

SOURCE_SHA256 = "eef88481073ab4ff998b3446d685322b92bd2ef5a0e090d00f9e46bb814c15ad"
DIST_INFO = "webull_openapi_python_sdk-2.0.15.dist-info"
METADATA = f"{DIST_INFO}/METADATA"
RECORD = f"{DIST_INFO}/RECORD"
OLD = ('Requires-Dist: cryptography<49,>=48.0.1; '
       'python_version >= "3.12" and python_version < "3.14"')
NEW = ('Requires-Dist: cryptography<55,>=50.0.0; '
       'python_version >= "3.12" and python_version < "3.14"')


def _digest(data: bytes) -> str:
    value = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return "sha256=" + value.rstrip(b"=").decode("ascii")


def upgrade(source: Path, destination: Path) -> str:
    raw = source.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != SOURCE_SHA256:
        raise ValueError(f"source wheel SHA-256 mismatch: {actual}")
    with zipfile.ZipFile(io.BytesIO(raw)) as original:
        infos = {item.filename: item for item in original.infolist()}
        payloads = {name: original.read(name) for name in original.namelist()
                    if name != RECORD}
    metadata = payloads[METADATA].decode("utf-8")
    if metadata.count(OLD) != 1:
        raise ValueError("expected Python 3.12 cryptography constraint not found")
    payloads[METADATA] = metadata.replace(OLD, NEW).encode("utf-8")
    rows = [(name, _digest(data), str(len(data)))
            for name, data in payloads.items()]
    rows.append((RECORD, "", ""))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    payloads[RECORD] = output.getvalue().encode("utf-8")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as wheel:
        for name, data in payloads.items():
            wheel.writestr(infos.get(name, zipfile.ZipInfo(name)), data)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: upgrade_webull_wheel_cryptography.py SOURCE DEST")
    print(upgrade(Path(sys.argv[1]), Path(sys.argv[2])))
