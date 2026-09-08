"""Bounded FIFO stress proof against the real RTDB emulator, never cloud."""
from __future__ import annotations

import json
import os
import sys
import uuid
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import firebase_admin
from firebase_admin import db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def encoded_bytes(value) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def main() -> None:
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST"):
        raise SystemExit("FIREBASE_DATABASE_EMULATOR_HOST is required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-fifo")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project,
    })
    from bounded_fifo import (MATCH_PAGES_PER_CALL_V3, PROJECTION_LIMIT_V3,
                              make_page, page_key)
    from lego_state import (REALIZED_FIFO_SCHEMA_VERSION,
                            REALIZED_HOT_EVENT_LIMIT,
                            REALIZED_LOT_PAGE_PATH, REALIZED_PATH,
                            apply_realized_fill)

    nonce = uuid.uuid4().hex[:10]
    measurements = []
    chains = []
    try:
        for count in (100, 1000, 10000):
            chain = f"fifo_{count}_{nonce}"
            chains.append(chain)
            pages = {}
            projection = []
            for index in range(count):
                price = 90.0 + (index % 997) / 1000.0
                page = make_page(
                    sequence=index, side_key="buys", quantity=1.0,
                    price=price, fee_per_share=0.01,
                    event_id=f"seed-{index:05d}")
                pages[page_key(index)] = page
                if index < PROJECTION_LIMIT_V3:
                    projection.append([1.0, price, 0.01])
            db.reference(
                f"{REALIZED_LOT_PAGE_PATH}/{chain}/buys").set(pages)
            head = {
                "schema_version": REALIZED_FIFO_SCHEMA_VERSION,
                "ledger_sequence": 0,
                "fifo_read_cursor": {"buys": 0, "sells": 0},
                "fifo_write_cursor": {"buys": count, "sells": 0},
                "fifo_head_remaining": None,
                "active_matching_event_id": None,
                "matching_progress": None,
                "projection_repair_cursor": 0,
                "open_legs": {"buys": projection, "sells": []},
                "applied_fills": {},
                "applied_seq": 0,
                "cumulative_realized": 0.0,
            }
            db.reference(f"{REALIZED_PATH}/{chain}").set(head)
            direct_page = db.reference(
                f"{REALIZED_LOT_PAGE_PATH}/{chain}/buys/{page_key(count - 1)}"
            ).get()
            measurements.append({
                "unmatched_lots": count,
                "hot_head_bytes": encoded_bytes(head),
                "direct_page_bytes": encoded_bytes(direct_page),
                "projection_lots": len(projection),
            })

        chain = chains[-1]
        cursor_history = [0]
        result = {"matching_pending": True}
        calls = 0
        page_reads_per_call = []
        original_get = type(db.reference("/")).get
        page_reads = []

        def measured_get(reference, *args, **kwargs):
            if reference.path.startswith(f"/{REALIZED_LOT_PAGE_PATH}/"):
                # Reject whole-history reads, including generation directories.
                assert len(reference.path.rsplit("/", 1)[-1]) == 20
                page_reads.append(reference.path)
            return original_get(reference, *args, **kwargs)

        while result["matching_pending"]:
            page_reads.clear()
            with patch.object(type(db.reference("/")), "get", measured_get):
                result = apply_realized_fill(
                    chain, "multi-page-sell", "SELL", 65, 120, 0.65)
            page_reads_per_call.append(len(page_reads))
            assert len(page_reads) <= 2 * MATCH_PAGES_PER_CALL_V3
            calls += 1
            cursor = int(db.reference(
                f"{REALIZED_PATH}/{chain}/fifo_read_cursor/buys").get())
            cursor_history.append(cursor)
            assert cursor - cursor_history[-2] <= MATCH_PAGES_PER_CALL_V3
            assert calls < 20
        final_head = db.reference(f"{REALIZED_PATH}/{chain}").get()
        assert calls == 9
        assert cursor_history[-1] == 65
        assert len(final_head["open_legs"]["buys"]) <= PROJECTION_LIMIT_V3
        assert len(final_head["applied_fills"]) <= REALIZED_HOT_EVENT_LIMIT
        assert encoded_bytes(final_head) < 65536
        assert max(item["hot_head_bytes"] for item in measurements) < 65536
        assert max(item["hot_head_bytes"] for item in measurements) \
            - min(item["hot_head_bytes"] for item in measurements) < 256
        print(json.dumps({
            "status": "PASS",
            "real_rtdb_emulator": True,
            "workloads": measurements,
            "matching_steps_per_call_limit": MATCH_PAGES_PER_CALL_V3,
            "direct_page_reads_per_call_limit": 2 * MATCH_PAGES_PER_CALL_V3,
            "measured_direct_page_reads_per_call": page_reads_per_call,
            "multi_page_match_calls": calls,
            "cursor_history": cursor_history,
            "final_hot_head_bytes": encoded_bytes(final_head),
            "history_fetches": 0,
            "operator_knobs_added": 0,
        }, sort_keys=True))

        race_chain = f"fifo_race_{nonce}"
        chains.append(race_chain)
        race_pages = {}
        race_projection = []
        prices = []
        for index in range(80):
            price = 95.0 + index / 100.0
            prices.append(price)
            page = make_page(
                sequence=index, side_key="buys", quantity=1.0,
                price=price, fee_per_share=0.01,
                event_id=f"race-seed-{index:03d}")
            race_pages[page_key(index)] = page
            if index < PROJECTION_LIMIT_V3:
                race_projection.append([1.0, price, 0.01])
        witnesses = {
            f"old-{index:03d}": {
                "quantity": 1.0, "fee": 0.0, "average_price": 100.0,
                "side": "BUY", "realized_delta": 0.0,
                "cumulative_realized_after": 0.0,
                "open_legs_after_hash": "seed", "seq": index + 1,
            }
            for index in range(REALIZED_HOT_EVENT_LIMIT)
        }
        db.reference(
            f"{REALIZED_LOT_PAGE_PATH}/{race_chain}/buys").set(race_pages)
        db.reference(f"{REALIZED_PATH}/{race_chain}").set({
            "schema_version": REALIZED_FIFO_SCHEMA_VERSION,
            "ledger_sequence": 0,
            "fifo_read_cursor": {"buys": 0, "sells": 0},
            "fifo_write_cursor": {"buys": 80, "sells": 0},
            "fifo_head_remaining": None,
            "active_matching_event_id": None,
            "matching_progress": None,
            "projection_repair_cursor": 0,
            "open_legs": {"buys": race_projection, "sells": []},
            "applied_fills": witnesses,
            "applied_seq": REALIZED_HOT_EVENT_LIMIT,
            "cumulative_realized": 0.0,
        })
        with ThreadPoolExecutor(max_workers=16) as pool:
            concurrent_results = list(pool.map(
                lambda _i: apply_realized_fill(
                    race_chain, "sell-race", "SELL", 80, 120, 0.8),
                range(16)))
        race_result = concurrent_results[-1]
        while race_result["matching_pending"]:
            race_result = apply_realized_fill(
                race_chain, "sell-race", "SELL", 80, 120, 0.8)
        oracle = sum((120.0 - p) - 0.01 - 0.01 for p in prices)
        assert abs(race_result["realized_cumulative"] - oracle) < 1e-8
        with ThreadPoolExecutor(max_workers=16) as pool:
            corrections = list(pool.map(
                lambda _i: apply_realized_fill(
                    race_chain, "sell-race", "SELL", 80, 120, 1.6),
                range(16)))
        corrected_head = db.reference(f"{REALIZED_PATH}/{race_chain}").get()
        assert abs(float(corrected_head["cumulative_realized"])
                   - (oracle - 0.8)) < 1e-8
        old_replay = apply_realized_fill(
            race_chain, "old-000", "BUY", 1, 100, 0)
        assert old_replay["realized_delta"] == 0
        print(json.dumps({
            "status": "PASS",
            "real_rtdb_emulator": True,
            "concurrency": 16,
            "concurrent_fifo_event": "sell-race",
            "fifo_pages_consumed": 80,
            "oracle_realized": oracle,
            "stored_realized_after_fee_correction": float(
                corrected_head["cumulative_realized"]),
            "fee_correction_delta": -0.8,
            "hot_witnesses": len(corrected_head["applied_fills"]),
            "old_replay_delta": old_replay["realized_delta"],
        }, sort_keys=True))
    finally:
        for chain in chains:
            db.reference(f"{REALIZED_PATH}/{chain}").delete()
            db.reference(f"{REALIZED_LOT_PAGE_PATH}/{chain}").delete()


if __name__ == "__main__":
    main()
