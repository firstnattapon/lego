"""Write and verify a DNA bundle (LEGO_DNA_BUNDLE) without hand-computing hashes.

    python -m tools.make_dna_bundle --dna-code bypass:6500 \
        --origin 2026-09-08T13:30:00Z --interval 900 --output strategy.uat-continuous.json

The bundle is validated with config.DNABundle.from_mapping before it is written,
and an existing file is never overwritten. The output reproduces the format of
strategy.example.json byte for byte (see test_make_dna_bundle.py).

A different dna_code is a different chain (config_hash/chain_key change), so use
this only for a deliberate DNA change. `bypass:N` has no gate: N only sets how
long the DNA lasts, so the same strategy runs for N market slots.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy

from config import DNABundle
from dna_engine import decode_dna, dna_fingerprint
from market_clock import MarketClockError, calendar_fingerprint, is_regular_session
from operational_health import dna_end


def build_bundle(dna_code: str, origin_utc: str, interval_seconds: int) -> dict:
    origin = datetime.fromisoformat(origin_utc.replace("Z", "+00:00"))
    if origin.tzinfo is None:
        raise ValueError("origin must include a timezone, e.g. 2026-09-08T13:30:00Z")
    if not is_regular_session(origin.astimezone(timezone.utc)):
        raise ValueError("origin must be inside a regular New York market session")
    # The fingerprint covers the slot size and the origin; market_clock reads both
    # from the environment, exactly as main._run_tick sets them from the bundle.
    previous = {key: os.environ.get(key) for key in ("LEGO_SLOT_SECONDS", "LEGO_DNA_ORIGIN_UTC")}
    os.environ["LEGO_SLOT_SECONDS"] = str(int(interval_seconds))
    os.environ["LEGO_DNA_ORIGIN_UTC"] = origin_utc
    try:
        calendar = calendar_fingerprint()
    except MarketClockError as exc:                 # unsupported slot size
        raise ValueError(str(exc)) from exc
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    raw = {
        "dna_code": dna_code, "interval_seconds": int(interval_seconds),
        "origin_utc": origin_utc, "calendar_id": "XNYS-regular",
        "calendar_fingerprint": calendar,
        "decoder_version": f"legacy-v1-numpy-{numpy.__version__}",
        "decoded_array_sha256": dna_fingerprint(dna_code),
    }
    DNABundle.from_mapping(raw)                    # same validation the runtime applies
    return raw


def write_bundle(path: Path, raw: dict) -> str:
    text = json.dumps(raw, indent=2) + "\n"
    with path.open("x", encoding="utf-8") as handle:     # refuses to overwrite
        handle.write(text)
    return hashlib.sha256(text.encode()).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dna-code", required=True)
    parser.add_argument("--origin", required=True, help="UTC slot start inside a regular session")
    parser.add_argument("--interval", type=int, default=900, help="slot seconds (default 900)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = build_bundle(args.dna_code, args.origin, args.interval)
    except ValueError as exc:    # ConfigurationError and DNAError are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        digest = write_bundle(args.output, raw)
    except FileExistsError:
        print(f"refusing to overwrite {args.output}; choose a new file name", file=sys.stderr)
        return 2
    length = len(decode_dna(args.dna_code))
    print(json.dumps({
        "output": str(args.output), "file_sha256": digest, "dna_code": args.dna_code,
        "length": length, "dna_end_utc": dna_end(
            args.origin, length, args.interval, raw["calendar_fingerprint"]),
        "calendar_fingerprint": raw["calendar_fingerprint"],
        "decoded_array_sha256": raw["decoded_array_sha256"],
        "note": "a new dna_code starts a new chain; plan the release with ops.py release-plan"},
        indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
