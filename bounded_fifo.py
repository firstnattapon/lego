"""Bounded, resumable FIFO lot matching primitives for the RTDB realized ledger."""
from __future__ import annotations

import hashlib
import json
import math


SCHEMA_VERSION = 3
PAGE_SIZE = 1
MATCH_PAGES_PER_CALL_V3 = 8
PROJECTION_LIMIT_V3 = 16
MIGRATION_PAGES_PER_CALL_V3 = 64


def page_key(sequence: int) -> str:
    if type(sequence) is not int or sequence < 0:
        raise ValueError("FIFO page sequence must be an integer >= 0")
    return f"{sequence:020d}"


def page_hash(page: dict) -> str:
    raw = json.dumps(page, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make_page(*, sequence: int, side_key: str, quantity: float,
              price: float, fee_per_share: float, event_id: str) -> dict:
    page = {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "side": side_key,
        "quantity": quantity,
        "price": price,
        "fee_per_share": fee_per_share,
        "event_id": str(event_id),
    }
    page["content_hash"] = page_hash(page)
    return page


def validate_page(page: object, *, sequence: int, side_key: str) -> dict:
    if not isinstance(page, dict):
        raise ValueError("FIFO lot page missing")
    doc = dict(page)
    claimed = str(doc.pop("content_hash", ""))
    if claimed != page_hash(doc):
        raise ValueError("FIFO lot page content hash mismatch")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("FIFO lot page schema mismatch")
    if doc.get("sequence") != sequence or doc.get("side") != side_key:
        raise ValueError("FIFO lot page cursor identity mismatch")
    for field in ("quantity", "price", "fee_per_share"):
        value = doc.get(field)
        if isinstance(value, bool):
            raise ValueError(f"FIFO lot {field} invalid")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"FIFO lot {field} invalid") from exc
        if not math.isfinite(number):
            raise ValueError(f"FIFO lot {field} invalid")
        doc[field] = number
    if doc["quantity"] <= 0 or doc["price"] <= 0 or doc["fee_per_share"] < 0:
        raise ValueError("FIFO lot values outside allowed range")
    doc["content_hash"] = claimed
    return doc
