"""Race the real lego_tick/worker orchestration on RTDB emulator.

Only Webull/network and wall-clock boundaries are stubbed. Decision creation,
row/intent persistence, dispatch claims, place fence, worker, and lego_tick are
production functions.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import firebase_admin
from firebase_admin import db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Request:
    def get_json(self, silent=True):
        return None


class FixedDateTime(datetime):
    moment = datetime(2026, 9, 4, 15, 0, 5, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.moment if tz is None else cls.moment.astimezone(tz)


def main() -> None:
    if not os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST"):
        raise SystemExit("FIREBASE_DATABASE_EMULATOR_HOST is required")
    project = os.environ.get("GCLOUD_PROJECT", "demo-lego-tick-race")
    firebase_admin.initialize_app(options={
        "databaseURL": f"https://{project}-default-rtdb.firebaseio.com",
        "projectId": project,
    })

    import decision_service
    import execution_service
    import main as app
    from config import load_runtime_config
    from lego_outbox import (DISPATCH_LOCK_PATH, OUTBOX_PATH,
                             account_symbol_fence_key)
    from lego_state import ROWS_PATH, STATE_PATH, chain_key
    from market_clock import resolve_market_slot
    from webull_io import InstrumentCapability

    nonce = uuid.uuid4().hex[:10]
    env = {
        "LEGO_SYMBOL": "AAPL",
        "LEGO_FIX_C": "1500",
        "LEGO_DIFF": "25",
        "LEGO_DNA_CODE": "bypass:100000",
        "LEGO_MODE": "trade",
        "LEGO_ACTIVE": "true",
        "WEBULL_ENV": "UAT",
        "WEBULL_ACCOUNT_ID": f"emulator-account-{nonce}",
        "GOOGLE_CLOUD_PROJECT": project,
        "FIREBASE_DB_URL": f"https://{project}-default-rtdb.firebaseio.com",
        "LEGO_CANDIDATE_HASH": f"emulator-candidate-{nonce}",
        "LEGO_MAX_ORDER_QUANTITY": "100",
        "LEGO_MAX_ORDER_NOTIONAL_USD": "2000",
        "LEGO_MAX_SESSION_ORDERS": "1",
        "LEGO_TRADING_WINDOW_END": "2030-01-01T00:00:00Z",
    }
    staged = load_runtime_config(env)
    env["LEGO_RELEASE_AUTHORIZATION"] = staged.deployment.expected_release_binding
    runtime = load_runtime_config(env)
    os.environ["LEGO_SLOT_SECONDS"] = str(
        runtime.operator.dna_bundle.interval_seconds)
    os.environ["LEGO_DNA_ORIGIN_UTC"] = "2026-09-04T13:30:00Z"
    os.environ["LEGO_DNA_CLOCK_MODE"] = "market"
    os.environ["WEBULL_ACCOUNT_ID"] = env["WEBULL_ACCOUNT_ID"]
    os.environ["WEBULL_ENV"] = env["WEBULL_ENV"]
    identity = runtime.deployment.account_fingerprint
    cfg = app.Config(
        symbol="AAPL", fix_c=1500.0, diff=25.0,
        dna_code="bypass:100000", strategy_id="shannon_demon_lego_v2")
    ck = chain_key(cfg)
    scope = account_symbol_fence_key(identity, "AAPL")
    slot = resolve_market_slot(FixedDateTime.moment)
    assert slot is not None
    snapshot = {
        "captured_at": FixedDateTime.moment.isoformat().replace("+00:00", "Z"),
        "quote_time": FixedDateTime.moment.isoformat().replace("+00:00", "Z"),
        "price": 100.0,
        "holdings": 0.0,
    }
    capability = InstrumentCapability(
        symbol="AAPL", status="OC", category="US_STOCK", currency="USD",
        lot_size=Decimal("1"), fractionable=False)
    placed = []
    placed_lock = threading.Lock()

    def place(_client, order):
        with placed_lock:
            placed.append(order)
        run_id = str(order[0]["client_order_id"])
        return {"client_order_id": run_id, "order_id": f"broker-{run_id}"}

    def detail(_client, run_id):
        return {
            "client_order_id": run_id,
            "symbol": "AAPL",
            "order_status": "SUBMITTED",
            "filled_quantity": "0",
        }

    # Composition/runtime boundaries.
    app.load_runtime_config = lambda: runtime
    app.runtime_identity_fingerprint = lambda: identity
    execution_service._init_firebase = lambda: None
    decision_service._init_firebase = lambda: None
    decision_service.datetime = FixedDateTime
    execution_service.datetime = FixedDateTime
    decision_service.resolve_market_slot = lambda _moment: slot
    decision_service.is_regular_session = lambda _moment: True
    decision_service.slot_seconds = lambda: runtime.operator.dna_bundle.interval_seconds
    decision_service.clock_mode = lambda: "market"
    decision_service.market_category = lambda: "US_STOCK"
    decision_service.environment_label = lambda: "Test (UAT)"
    decision_service.runtime_identity_fingerprint = lambda: identity
    decision_service.build_clients = lambda: (object(), object())
    decision_service.fetch_instrument_capability = lambda *_args: capability
    decision_service.fetch_snapshot = lambda *_args: dict(snapshot)
    decision_service.token_health = lambda: {"ok": True, "reasons": []}

    # Broker adapter boundaries; worker/state/claims remain production code.
    execution_service.build_clients = lambda: (object(), object())
    execution_service.environment_label = lambda: "Test (UAT)"
    execution_service.fetch_open_orders = lambda *_args: []
    execution_service.fetch_snapshot = lambda *_args: dict(snapshot)
    execution_service.fetch_buying_power = lambda *_args: Decimal("100000")
    execution_service.preview_market_order_result = lambda *_args, **_kw: {
        "estimated_cost": "1500", "estimated_transaction_fee": "0"}
    execution_service.place_market_order = place
    execution_service.fetch_order_detail = detail
    execution_service.ORDER_POLL_ATTEMPTS = 1
    execution_service.time.sleep = lambda _seconds: None

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _i: app.lego_tick(Request()), range(16)))
        rows = db.reference(ROWS_PATH).get() or {}
        intents = db.reference(f"{OUTBOX_PATH}/{ck}").get() or {}
        state = db.reference(f"{STATE_PATH}/{ck}").get() or {}
        assert len(rows) == 1, (
            f"committed rows={len(rows)} results="
            f"{json.dumps(results, default=str, ensure_ascii=True)}")
        assert len(intents) == 1, f"intents={len(intents)}"
        assert len(placed) == 1, f"place calls={len(placed)}"
        assert int(state["version"]) == 1
        run_id = next(iter(intents))
        assert placed[0][0]["client_order_id"] == run_id
        print(json.dumps({
            "status": "PASS",
            "real_rtdb_emulator": True,
            "concurrency": 16,
            "lego_tick_invocations": len(results),
            "http_200": sum(code == 200 for _body, code in results),
            "committed_rows": len(rows),
            "intents": len(intents),
            "broker_place_calls": len(placed),
            "stubbed_scope": "broker/network/time boundaries only",
        }, sort_keys=True))
    finally:
        db.reference(ROWS_PATH).delete()
        db.reference(f"{STATE_PATH}/{ck}").delete()
        db.reference(f"{OUTBOX_PATH}/{ck}").delete()
        db.reference(f"{DISPATCH_LOCK_PATH}/{scope}").delete()


if __name__ == "__main__":
    main()
