"""Rebuild Webull SDK 2.0.15 with only its cryptography metadata corrected.

The runtime package files are copied byte-for-byte.  The source wheel SHA-256 is
verified first, and RECORD is regenerated according to the wheel specification.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path


ORIGINAL_SHA256 = (
    "c9c5f4ee3c62c7ecbe45e4b1b19ebd944a04620e0aebd35851d2f8c41acc9a14"
)
DIST_INFO = "webull_openapi_python_sdk-2.0.15.dist-info"
METADATA = f"{DIST_INFO}/METADATA"
RECORD = f"{DIST_INFO}/RECORD"
OLD_PY312 = (
    'Requires-Dist: cryptography<43,>=41.0; '
    'python_version >= "3.12" and python_version < "3.14"'
)
NEW_PY312 = (
    'Requires-Dist: cryptography<49,>=48.0.1; '
    'python_version >= "3.12" and python_version < "3.14"'
)
OLD_PY314 = (
    'Requires-Dist: cryptography<55,>=43.0; python_version >= "3.14"'
)
NEW_PY314 = (
    'Requires-Dist: cryptography<55,>=48.0.1; python_version >= "3.14"'
)


def _digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def rebuild(source: Path, destination: Path) -> None:
    source_bytes = source.read_bytes()
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != ORIGINAL_SHA256:
        raise ValueError(
            f"source wheel SHA-256 mismatch: {actual} != {ORIGINAL_SHA256}")

    with zipfile.ZipFile(io.BytesIO(source_bytes)) as original:
        if any(name.endswith(("RECORD.jws", "RECORD.p7s"))
               for name in original.namelist()):
            raise ValueError("signed wheel is not supported")
        infos = {info.filename: info for info in original.infolist()}
        payloads = {
            name: original.read(name)
            for name in original.namelist()
            if name != RECORD
        }

    metadata = payloads[METADATA].decode("utf-8")
    if metadata.count(OLD_PY312) != 1 or metadata.count(OLD_PY314) != 1:
        raise ValueError("expected cryptography constraints not found exactly once")
    metadata = metadata.replace(OLD_PY312, NEW_PY312).replace(
        OLD_PY314, NEW_PY314)
    payloads[METADATA] = metadata.encode("utf-8")

    rows = [
        (name, _digest(data), str(len(data)))
        for name, data in payloads.items()
    ]
    rows.append((RECORD, "", ""))
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(rows)
    payloads[RECORD] = record_buffer.getvalue().encode("utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED) as patched:
        for name, data in payloads.items():
            info = infos.get(name)
            if info is None:
                info = zipfile.ZipInfo(name)
            patched.writestr(info, data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    rebuild(args.source, args.destination)
    print(hashlib.sha256(args.destination.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
