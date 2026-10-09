"""The flight recorder: what it keeps, what it must never keep, and that it can never hurt a tick.

Origin: 2026-10-08 a UAT order sat PENDING and the DAY-expiry proof never released it, but
nothing durable said why (the reason was a ``logger.info`` Cloud Logging never received).
These tests pin the recorder that makes the next such incident readable from the database.
"""
from __future__ import annotations

import ast
import json
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from requests.structures import CaseInsensitiveDict

import flight_recorder as fr
import security_text
import tick_runtime
import webull_io
from conftest import FAKE_DB

TICK = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CHAIN = "UBER_test0001"
DAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    FAKE_DB.store.clear()
    for name in ("LEGO_TRACE_LEVEL", "LEGO_TRACE_BODIES", "LEGO_TRACE_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "acct-9876543210")
    monkeypatch.setenv("WEBULL_APP_KEY", "sentinel-app-key-value")
    monkeypatch.setenv("WEBULL_APP_SECRET", "sentinel-app-secret-value")
    yield
    FAKE_DB.store.clear()


@contextmanager
def tick(tick_id=TICK, **meta):
    token = fr.begin(tick_id)
    fr.bind(chain_key=CHAIN, symbol="UBER", env="UAT", mode="trade", **meta)
    try:
        yield
    finally:
        fr.reset(token)


def traces():
    return FAKE_DB.reference(f"{fr.TRACE_PATH}/{CHAIN}/{DAY}").get() or {}


def only_trace():
    found = traces()
    assert len(found) == 1, found
    return next(iter(found.values()))


def body(**over):
    base = {"pipeline_status": "TICK_OK", "business_status": "ROW_COMMITTED",
            "decision": {"status": "READY_BUY", "market_slot_id": "2026-10-08:7", "step": 579,
                         "committed": True}}
    base.update(over)
    return base


# ----------------------------------------------------------------------------- sanitizer

def test_trace_clean_never_keeps_credentials_or_account_identity():
    payload = {
        "account_id": "acct-9876543210",
        "x-signature": "sig-should-not-appear",
        "access_token": "tok-should-not-appear",
        "message": "order failed for acct-9876543210 with token=hunter2secret and sentinel-app-key-value",
        "nested": {"app_secret": "sentinel-app-secret-value", "ok": 1.5,
                   "rows": [{"symbol": "UBER", "quantity": "0.77"}]},
        "free text": "Bearer abcdef0123456789",
    }
    cleaned = security_text.trace_clean(payload)
    dumped = json.dumps(cleaned, ensure_ascii=False)
    for secret in ("acct-9876543210", "sig-should-not-appear", "tok-should-not-appear",
                   "hunter2secret", "sentinel-app-key-value", "sentinel-app-secret-value",
                   "abcdef0123456789"):
        assert secret not in dumped
    assert cleaned["nested"]["ok"] == 1.5
    assert cleaned["nested"]["rows"][0] == {"symbol": "UBER", "quantity": "0.77"}


def test_trace_clean_makes_keys_legal_for_the_database_and_never_mutates():
    payload = {"a.b/c": 1, "x[0]": 2, "y#z$": 3, "": 4, "ราคา Pₙ (USD)": 5, "k\n": 6}
    before = json.dumps(payload, ensure_ascii=False)
    cleaned = security_text.trace_clean(payload)
    assert json.dumps(payload, ensure_ascii=False) == before
    assert not any(ch in key for key in cleaned for ch in ".$#[]/\n")
    assert len(cleaned) == len(payload)                       # nothing collapsed silently
    assert cleaned["ราคา Pₙ (USD)"] == 5


def test_trace_clean_bounds_size_depth_and_non_finite_numbers():
    deep = current = {}
    for _ in range(30):
        current["d"] = {}
        current = current["d"]
    assert "<truncated>" in json.dumps(security_text.trace_clean(deep))
    wide = {f"k{i}": "x" * 1000 for i in range(1000)}
    cleaned = security_text.trace_clean(wide, max_nodes=50, max_str=40)
    assert len(cleaned) <= 52 and cleaned.get("_truncated") is True
    assert all(len(v) <= 41 for k, v in cleaned.items() if isinstance(v, str))
    odd = security_text.trace_clean({"a": float("nan"), "b": math.inf, "c": b"1234"})
    assert odd == {"a": "nan", "b": "inf", "c": "<bytes 4>"}


# ----------------------------------------------------------------------------- recording

def test_nodes_equations_and_path_follow_the_flow_chart():
    with tick():
        fr.node("D00", mode="market")
        fr.node("D05", slot="2026-10-08:7", market_ordinal=579)
        fr.equation("D08", {"P_n": 68.62, "holdings": 144.9528, "FIX_C": 10000.0},
                    {"status": "READY_BUY", "gap": 53.3, "qty": 0.77}, "gap = FIX_C - holdings*P_n")
        fr.node("D10", "committed", run_id="run-1")
        fr.node("W10", "PENDING", run_id="run-1")
        fr.node("W11", "order_still_listed_open")
        result = fr.finish(body(), 200)
    assert result["written"] is True
    envelope = only_trace()
    assert envelope["path"] == "D00>D05>D08[READY_BUY]>D10[committed]|W10[PENDING]>W11[order_still_listed_open]"
    assert envelope["runs"] == ["run-1"]
    equation = next(e for e in envelope["events"] if e["k"] == "eq")
    assert equation["in"]["P_n"] == 68.62 and equation["out"]["qty"] == 0.77
    assert equation["f"].startswith("gap =")


def test_every_catalog_node_has_a_group_title_and_meaning():
    assert {"D00", "D08", "D10", "W10", "W11", "W25", "H00", "X00"} <= set(fr.NODES)
    for node_id, info in fr.NODES.items():
        assert set(info) == {"group", "title", "what"} and all(info.values()), node_id
        assert info["group"] in {"decision", "worker", "ops"}


def _production_node_ids() -> set[str]:
    """Every node-id string literal in the production modules (the catalog's own keys excluded)."""
    found: set[str] = set()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = set()
        if path.name == "flight_recorder.py":
            for node in tree.body:
                targets = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
                target = getattr(node, "target", None)
                if "NODES" in targets or (isinstance(target, ast.Name) and target.id == "NODES"):
                    skipped.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped
                    and re.fullmatch(r"[DWTXHS]\d\d", node.value)):
                found.add(node.value)
    return found


def test_flight_recorder_catalog_matches_the_hooks_in_the_code():
    used = _production_node_ids()
    assert used == set(fr.NODES), (sorted(used - set(fr.NODES)), sorted(set(fr.NODES) - used))


def test_flight_recorder_catalog_matches_the_flow_chart_in_the_doc():
    doc = (Path(__file__).resolve().parent / "docs" / "FLIGHT_RECORDER_TH.md").read_text(encoding="utf-8")
    block = re.search(r"```mermaid\n(.*?)```", doc, re.S).group(1)
    assert set(re.findall(r"\b([A-Z]\d{2})\b", block)) == set(fr.NODES)
    for node_id, info in fr.NODES.items():              # the doc names the same things the code does
        assert re.search(rf'\b{node_id}\[?"?{node_id}\b|\b{node_id}\["{node_id} ', block), node_id


def test_envelope_identifies_the_deployment_and_the_first_record_carries_boot_env(monkeypatch):
    monkeypatch.setenv("K_REVISION", "lego-tick-uat-00007-xyz")
    monkeypatch.setenv("LEGO_CANDIDATE_HASH", "c" * 64)
    monkeypatch.setenv("LEGO_GIT_COMMIT", "49b8040")
    monkeypatch.setenv("LEGO_FIX_C", "10000")
    monkeypatch.setenv("LEGO_RELEASE_AUTHORIZATION", "release-binding-sentinel")
    with tick():
        fr.node("D10", "committed")
        fr.finish(body(), 200)
    with tick("b" * 32):
        fr.node("D10", "committed")
        fr.finish(body(), 200)
    first, second = sorted(traces().values(), key=lambda e: e["at"])
    assert first["rev"] == "lego-tick-uat-00007-xyz" and first["cand"] == "c" * 64
    assert first["git"] == "49b8040" and first["env"] == "UAT" and first["sym"] == "UBER"
    assert first["boot"]["env"]["LEGO_FIX_C"] == "10000"
    assert "boot" not in second
    dumped = json.dumps(first["boot"])
    assert "acct-9876543210" not in dumped and "sentinel-app-key-value" not in dumped
    assert "release-binding-sentinel" not in dumped and "LEGO_RELEASE_AUTHORIZATION" not in first["boot"]["env"]


def test_idle_ticks_write_no_trace_after_the_boot_record_and_only_a_throttled_heartbeat():
    for index in range(3):
        with tick(f"{index:032d}"):
            fr.finish({"pipeline_status": "TICK_OK", "business_status": "MARKET_CLOSED"}, 200)
    assert [e["why"] for e in traces().values()] == [["boot"]]      # only the first tick of a boot
    heartbeat = FAKE_DB.reference(f"{fr.HEARTBEAT_PATH}/{CHAIN}").get()
    assert heartbeat["ticks"] == 1 and heartbeat["biz"] == "MARKET_CLOSED"   # 2nd/3rd within 5 min
    assert heartbeat["boot_id"] == fr._BOOT_ID
    fr._STATS["hb_mono"] -= fr.HEARTBEAT_SECONDS + 1                # five minutes later
    with tick("9" * 32):
        fr.finish({"pipeline_status": "TICK_OK", "business_status": "MARKET_CLOSED"}, 200)
    assert len(traces()) == 1
    assert FAKE_DB.reference(f"{fr.HEARTBEAT_PATH}/{CHAIN}").get()["ticks"] == 4


def test_a_tick_with_an_order_mutation_is_notable_even_when_the_pipeline_is_quiet():
    with tick():
        fr.webull_exchange("place", "/trading/orders/place", "POST", 812.0, status=200,
                           request_id="req-1", body={"new_orders": [{"symbol": "UBER"}]},
                           payload={"order_id": "0388X"})
        result = fr.finish({"pipeline_status": "TICK_OK", "business_status": "SLOT_CONSUMED"}, 200)
    assert result["written"] and "mutation" in result["reasons"]


def test_unresolved_intent_ticks_are_written_once_then_deduplicated_until_something_changes(monkeypatch):
    def waiting(status="CANCEL_UNKNOWN", blockers=None):
        item = {"run_id": "run-1", "status": status, "broker_status": "PENDING",
                "reconciliation_age_seconds": time.time()}
        if blockers:
            item["expiry_proof_blockers"] = blockers
        return {"pipeline_status": "TICK_DEFERRED", "business_status": "RECONCILIATION_OVERDUE",
                "decision": {"status": "PASS_SLOT_CONSUMED"}, "recovery": {"results": [item]}}

    outcomes = []
    for index, kwargs in enumerate([{}, {}, {}, {"blockers": ["order_still_listed_open"]},
                                    {"blockers": ["order_still_listed_open"]}]):
        with tick(f"{index:032d}"):
            outcomes.append(fr.finish(waiting(**kwargs), 200)["skipped"])
    assert outcomes == [None, "duplicate", "duplicate", None, "duplicate"]
    monkeypatch.setattr(fr, "DEDUP_SECONDS", 0.0)                    # window over -> checkpoint
    with tick("f" * 32):
        assert fr.finish(waiting(blockers=["order_still_listed_open"]), 200)["skipped"] is None


def test_row_commits_and_errors_are_never_deduplicated():
    for index in range(3):
        with tick(f"{index:032d}"):
            assert fr.finish(body(), 200)["written"]
    with tick("e" * 32):
        fr.error("unit", RuntimeError("boom with acct-9876543210"))
        assert fr.finish({"pipeline_status": "TICK_ERROR", "business_status": "ERROR"}, 500)["written"]
    envelope = next(e for e in traces().values() if e["tick"] == "e" * 32)
    assert "error" in envelope["why"]
    assert "acct-9876543210" not in json.dumps(envelope)


def test_level_off_is_a_kill_switch_and_level_all_keeps_idle_ticks(monkeypatch):
    monkeypatch.setenv("LEGO_TRACE_LEVEL", "off")
    assert fr.begin(TICK) is None and not fr.active()
    fr.node("D00")                                                    # no-op without a trace
    assert fr.finish(body(), 200) is None and FAKE_DB.store == {}
    monkeypatch.setenv("LEGO_TRACE_LEVEL", "all")
    with tick():
        result = fr.finish({"pipeline_status": "TICK_OK", "business_status": "MARKET_CLOSED"}, 200)
    assert result["written"] and result["reasons"] == ["boot", "level_all"]


def test_warnings_transitions_and_phases_are_recorded_and_sdk_phases_are_not_duplicated():
    with tick():
        fr.warning("expiry_proof_pending", "proof not met acct-9876543210", {"blockers": ["x"]})
        fr.transition(CHAIN, "run-1", "PENDING", "CANCEL_REQUESTED", {"cancel_attempt_count": 1})
        fr.phase("place", "PLACING_UNKNOWN", "ok", 12.5, 20000.0, {"request_id": "r-1"})
        fr.phase("sdk_place", None, "ok", 12.0, 20000.0)
        fr.finish(body(), 200)
    kinds = [e["k"] for e in only_trace()["events"]]
    assert kinds == ["wn", "tr", "ph"]
    assert "acct-9876543210" not in json.dumps(only_trace())
    assert only_trace()["runs"] == ["run-1"]


# ----------------------------------------------------------------------------- Webull exchanges

def test_webull_exchange_keeps_request_response_ids_and_hash():
    payload = {"order_id": "0388X", "status": "PENDING", "account_id": "acct-9876543210"}
    with tick():
        fr.webull_exchange("order_detail", "/trading/orders/get", "GET", 701.3, status=200,
                           request_id="9f1c0c6e-0000-4000-8000-000000000001",
                           query={"account_id": "acct-9876543210", "client_order_id": "run-1"},
                           payload=payload)
        fr.finish(body(), 200)
    event = next(e for e in only_trace()["events"] if e["k"] == "wb")
    assert event["op"] == "order_detail" and event["st"] == 200 and event["ms"] == 701.3
    assert event["rid"] == "9f1c0c6e-0000-4000-8000-000000000001"
    assert event["req"]["q"]["client_order_id"] == "run-1"
    assert event["res"]["status"] == "PENDING" and event["res"]["account_id"] == "<redacted>"
    assert event["req"]["q"]["account_id"] == "<redacted>"
    assert event["sha"] == fr._sha(payload) and event["len"] > 0
    assert event["fact"] == {"st": "PENDING"}
    assert "acct-9876543210" not in json.dumps(only_trace())


def test_webull_error_is_recorded_with_code_http_and_request_id():
    with tick():
        fr.webull_exchange("cancel", "/trading/orders/cancel", "POST", 395.0, status=417,
                           request_id="00000000-0000-4000-8000-000000000417",
                           body={"client_order_id": "run-1"},
                           error_info={"code": "OPENAPI_ORDER_CANNOT_OPERATE", "http": 417,
                                       "msg": "The current status cannot be modified."})
        result = fr.finish({"pipeline_status": "TICK_DEFERRED",
                            "business_status": "RECONCILIATION_OVERDUE"}, 200)
    event = next(e for e in only_trace()["events"] if e["k"] == "wb")
    assert event["err"]["code"] == "OPENAPI_ORDER_CANNOT_OPERATE" and event["err"]["http"] == 417
    assert event["rid"] == "00000000-0000-4000-8000-000000000417" and event["mut"] is True
    assert {"mutation", "webull_error"} <= set(result["reasons"])


def test_projection_keeps_only_the_configured_symbol_and_counts_the_rest():
    positions = {"positions": [
        {"symbol": "AAPL", "instrument_type": "EQUITY", "quantity": "10"},
        {"symbol": "UBER", "instrument_type": "EQUITY", "quantity": "144.9528"},
        {"symbol": "TSLA", "instrument_type": "EQUITY", "quantity": "3"}]}
    open_orders = {"data": [
        {"items": [{"symbol": "UBER", "client_order_id": "run-1", "status": "PENDING"},
                   {"symbol": "MSFT", "client_order_id": "other", "status": "WORKING"}]},
        {"symbol": "NVDA", "client_order_id": "x", "status": "WORKING"}], "pagination_key": "n"}
    with tick():
        fr.webull_exchange("positions", "/trading/assets/positions/list", "GET", 5.0, status=200,
                           payload=positions)
        fr.webull_exchange("open_orders", "/trading/orders/open-orders/list", "GET", 5.0, status=200,
                           payload=open_orders)
        fr.webull_exchange("accounts", "/trading/accounts/list", "GET", 5.0, status=200,
                           payload=[{"account_id": "acct-9876543210"}, {"account_id": "other"}])
        fr.finish(body(), 200)
    positions_event, orders_event, accounts_event = [e for e in only_trace()["events"] if e["k"] == "wb"]
    assert positions_event["res"]["count"] == 3
    assert [r["symbol"] for r in positions_event["res"]["rows"]] == ["UBER"]
    assert orders_event["res"]["count"] == 3 and orders_event["res"]["next"] is True
    assert [r["client_order_id"] for r in orders_event["res"]["rows"]] == ["run-1"]
    assert accounts_event["res"] == {"count": 2}
    assert "AAPL" not in json.dumps(only_trace()) and "TSLA" not in json.dumps(only_trace())


def test_auth_routes_keep_status_only_and_min_bodies_keep_hashes_only(monkeypatch):
    secret_payload = {"access_token": "tok-should-not-appear", "status": "NORMAL"}
    with tick():
        fr.webull_exchange("sdk_request", "/openapi/auth/token/check", "POST", 3.0, status=200,
                           payload=secret_payload, body={"token": "tok-should-not-appear"})
        fr.finish(body(), 200)
    event = next(e for e in only_trace()["events"] if e["k"] == "wb")
    assert "res" not in event and "req" not in event and "sha" not in event
    assert "tok-should-not-appear" not in json.dumps(only_trace())
    FAKE_DB.store.clear()
    fr.reset_state()
    monkeypatch.setenv("LEGO_TRACE_BODIES", "min")
    with tick():
        fr.webull_exchange("order_detail", "/trading/orders/get", "GET", 3.0, status=200,
                           query={"client_order_id": "run-1"}, payload={"status": "PENDING"})
        fr.finish(body(), 200)
    event = next(e for e in only_trace()["events"] if e["k"] == "wb")
    assert "res" not in event and "req" not in event and event["sha"] and event["fact"] == {"st": "PENDING"}


# ----------------------------------------------------------------------------- bounds

def test_oversized_events_are_shrunk_not_lost_and_the_tick_stays_bounded():
    huge = {f"row{i}": "y" * 150 for i in range(400)}
    with tick():
        fr.node("D08", "x", **{"blob": huge})
        for index in range(400):
            fr.phase("ledger_persistence", None, "ok", 1.0, 1000.0)
        fr.node("D10", "committed")
        fr.finish(body(), 200)
    envelope = only_trace()
    assert envelope["n"] <= int(fr.MAX_EVENTS * 1.25) and envelope["dropped"] > 0
    assert envelope["bytes"] <= fr.MAX_TICK_BYTES * 1.25
    nodes = [e["n"] for e in envelope["events"] if e["k"] == "n"]
    assert nodes == ["D08", "D10"]                                    # important events survive
    assert all(len(json.dumps(e)) <= fr.MAX_EVENT_BYTES + 200 for e in envelope["events"])


def test_retention_prunes_one_expired_day_bucket_per_hour(monkeypatch):
    for day in ("2026-09-01", "2026-09-02", DAY):
        FAKE_DB.reference(f"{fr.TRACE_PATH}/{CHAIN}/{day}/x").set({"v": 1})
        FAKE_DB.reference(f"{fr.DAYS_PATH}/{CHAIN}/{day}").set(True)
    now = datetime.now(timezone.utc)
    assert fr.prune(CHAIN, now) == "2026-09-01"
    assert FAKE_DB.reference(f"{fr.TRACE_PATH}/{CHAIN}/2026-09-01").get() is None
    assert FAKE_DB.reference(f"{fr.TRACE_PATH}/{CHAIN}/2026-09-02").get() is not None
    assert fr.prune(CHAIN, now) is None                               # at most hourly
    fr._STATS["prune_mono"] = None
    assert fr.prune(CHAIN, now) == "2026-09-02"
    fr._STATS["prune_mono"] = None
    assert fr.prune(CHAIN, now) is None                               # today's bucket is kept
    assert FAKE_DB.reference(f"{fr.TRACE_PATH}/{CHAIN}/{DAY}").get() is not None


# ----------------------------------------------------------------------------- can never hurt a tick

def test_checkpoint_writes_a_partial_record_that_finish_overwrites():
    with tick():
        fr.node("D10", "committed", run_id="run-1")
        fr.webull_exchange("place", "/trading/orders/place", "POST", 700.0, status=200,
                           payload={"order_id": "0388X"})
        fr.checkpoint()
        partial = only_trace()
        assert partial["partial"] is True and partial["n"] == 2
        fr.node("W10", "PENDING")
        fr.finish(body(), 200)
    final = only_trace()                                              # same key, overwritten
    assert final["partial"] is False and final["n"] == 3


def test_recorder_errors_are_swallowed_and_counted(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("sanitizer broke")

    with tick():
        monkeypatch.setattr(security_text, "trace_clean", explode)
        assert fr.node("D00", "x", a=1) is None
        assert fr.equation("D08", {"a": 1}, {"b": 2}) is None
        assert fr.webull_exchange("order_detail", "/r", "GET", 1.0, payload={"a": 1}) is None
        assert fr.warning("k", "m") is None
        assert fr._CURRENT.get().errors >= 3


def test_a_broken_database_costs_nothing_but_a_counter(monkeypatch):
    class Broken:
        def reference(self, path=""):
            raise ConnectionError("rtdb down")

    monkeypatch.setattr(fr, "db", Broken())
    with tick():
        fr.node("D10", "committed")
        result = fr.finish(body(), 200)
    assert result["written"] is False and result["skipped"] == "write_error"
    assert fr._STATS["write_errors"] == 1


def test_the_final_write_is_time_boxed(monkeypatch):
    class Slow:
        def reference(self, path=""):
            time.sleep(0.5)
            return FAKE_DB.reference(path)

    monkeypatch.setattr(fr, "db", Slow())
    monkeypatch.setattr(fr, "FLUSH_TIMEBOX_SECONDS", 0.05)
    started = time.monotonic()
    with tick():
        fr.node("D10", "committed")
        result = fr.finish(body(), 200)
    assert time.monotonic() - started < 0.4
    assert result["skipped"] == "write_timeout" and fr._STATS["flush_timeouts"] == 1


def test_a_tick_with_little_budget_left_does_not_write(monkeypatch):
    monkeypatch.setattr(fr, "_remaining", lambda: 5.0)
    with tick():
        fr.node("D10", "committed")
        result = fr.finish(body(), 200)
    assert result["skipped"] == "budget" and traces() == {}


def test_the_trace_never_prints_and_never_leaks_into_the_next_tick(capsys):
    with tick("1" * 32):
        fr.node("D00")
        fr.finish(body(), 200)
    assert fr._CURRENT.get() is None and not fr.active()
    fr.node("D99")                                                    # outside any tick: no-op
    with tick("2" * 32):
        snapshot = fr.snapshot()
        assert snapshot["events"] == [] and snapshot["tick"] == "2" * 32
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# ----------------------------------------------------------------------------- tick_runtime seams

def test_phase_annotations_carry_only_validated_metadata(capsys):
    with tick_runtime.tick_scope("trace"):
        with tick_runtime.phase("sdk_order_detail"):
            tick_runtime.annotate(request_id="9f1c0c6e-0000-4000-8000-000000000001",
                                  http_status=200, error_code="op_x", junk="x", payload="{}")
            tick_runtime.annotate(request_id="bad id with spaces", http_status=99999,
                                  error_code="has space", http_status_extra=1)
        tick_runtime.annotate(request_id="outside-any-phase-0001")        # no open phase: ignored
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["request_id"] == "9f1c0c6e-0000-4000-8000-000000000001"
    assert event["http_status"] == 200 and event["error_code"] == "OP_X"
    assert "junk" not in event and "payload" not in event and "http_status_extra" not in event


def test_tick_scope_starts_and_clears_the_trace_and_phases_feed_it():
    with tick_runtime.tick_scope("z" * 32, seconds=30.0):
        assert fr.active() and 0 < fr._remaining() <= 30.0
        with tick_runtime.phase("place", witness="PLACING_UNKNOWN"):
            pass
        with tick_runtime.phase("sdk_place"):
            pass                                                      # the exchange event has it
        events = fr.snapshot()["events"]
    assert not fr.active() and fr._remaining() is None
    assert [e["op"] for e in events] == ["place"] and events[0]["w"] == "PLACING_UNKNOWN"


# ----------------------------------------------------------------------------- the SDK boundary

REQUEST_ID = "00000000-0000-4000-8000-000000000417"


class _Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        # what requests.Response really carries: a case-insensitive mapping
        self.headers = CaseInsensitiveDict({"x-request-id": REQUEST_ID})
        self._payload = payload

    def json(self):
        return self._payload


class _Request:
    """The slice of the SDK's ApiRequest that the boundary reads."""

    def __init__(self, action, method="GET", query=None, body=None):
        self._action, self._method, self._query, self._body = action, method, query or {}, body or {}

    def get_action_name(self): return self._action
    def get_method(self): return self._method
    def get_query_params(self): return self._query
    def get_body_params(self): return self._body
    def set_read_timeout(self, value): pass
    def set_connect_timeout(self, value): pass


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    webull_io.reset_clients()

    def call(request, *, reply=None, error=None):
        class Base:
            def get_response(self, req):
                if error is not None:
                    raise error
                return reply
        return webull_io._bounded_api_class(Base)().get_response(request)

    yield call
    webull_io.reset_clients()


def _printed_phase(capsys, name):
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return next(e for e in lines if e.get("phase") == name)


def test_every_webull_exchange_is_recorded_and_the_phase_log_gains_only_metadata(sdk, capsys):
    reply = _Response({"orders": [{"status": "PENDING", "filled_quantity": "0.000000",
                                   "account_id": "acct-9876543210"}]})
    with tick_runtime.tick_scope(TICK):
        fr.bind(chain_key=CHAIN, symbol="UBER")
        returned = sdk(_Request("/trading/orders/get", query={"account_id": "acct-9876543210",
                                                              "client_order_id": "run-1"}),
                       reply=reply)
        events = fr.snapshot()["events"]
    assert returned is reply                                          # the call is untouched
    event = next(e for e in events if e["k"] == "wb")
    assert event["op"] == "order_detail" and event["rt"] == "/trading/orders/get"
    assert event["st"] == 200 and event["rid"] == REQUEST_ID and event["m"] == "GET"
    assert event["req"]["q"] == {"account_id": "<redacted>", "client_order_id": "run-1"}
    assert event["res"]["orders"][0]["status"] == "PENDING" and event["fact"]["st"] == "PENDING"
    printed = _printed_phase(capsys, "sdk_order_detail")
    assert printed["request_id"] == REQUEST_ID and printed["http_status"] == 200
    assert "res" not in printed and "orders" not in json.dumps(printed)      # Cloud Logging: no payload
    assert "acct-9876543210" not in json.dumps(events)


def test_a_refused_cancel_keeps_code_http_message_and_request_id_and_checkpoints(sdk, capsys):
    from webull.core.exception.exceptions import ServerException
    refusal = ServerException("OPENAPI_ORDER_CANNOT_OPERATE",
                              "The current status cannot be modified.", 417, REQUEST_ID)
    with tick_runtime.tick_scope(TICK):
        fr.bind(chain_key=CHAIN, symbol="UBER")
        with pytest.raises(ServerException) as caught:
            sdk(_Request("/trading/orders/cancel", "POST", body={"client_order_id": "run-1"}),
                error=refusal)
        events = fr.snapshot()["events"]
        partial = only_trace()                                        # written before the tick ends
    assert caught.value is refusal and caught.value._lego_operation == "cancel"
    event = next(e for e in events if e["k"] == "wb")
    assert event["op"] == "cancel" and event["mut"] is True and event["st"] == 417
    assert event["err"]["code"] == "OPENAPI_ORDER_CANNOT_OPERATE" and event["err"]["http"] == 417
    assert "cannot be modified" in event["err"]["msg"] and event["rid"] == REQUEST_ID
    assert partial["partial"] is True and partial["n"] == 1
    printed = _printed_phase(capsys, "sdk_cancel")
    assert printed["outcome"] == "error" and printed["error_code"] == "OPENAPI_ORDER_CANNOT_OPERATE"
    assert printed["http_status"] == 417 and printed["request_id"] == REQUEST_ID


def test_real_sdk_request_objects_are_read_through_their_own_accessors(sdk):
    from webull.trade.request.v3.place_order_request import PlaceOrderRequest
    request = PlaceOrderRequest()
    request.set_account_id("acct-9876543210")
    request.set_new_orders([{"client_order_id": "run-1", "symbol": "UBER", "side": "BUY",
                             "order_type": "MARKET", "quantity": "0.77", "time_in_force": "DAY"}])
    with tick_runtime.tick_scope(TICK):
        fr.bind(chain_key=CHAIN, symbol="UBER")
        sdk(request, reply=_Response({"order_id": "0388X", "client_order_id": "run-1"}))
        event = next(e for e in fr.snapshot()["events"] if e["k"] == "wb")
    assert event["op"] == "place" and event["m"] == "POST" and event["mut"] is True
    sent = event["req"]["b"]
    assert sent["account_id"] == "<redacted>"
    assert sent["new_orders"][0]["quantity"] == "0.77" and sent["new_orders"][0]["symbol"] == "UBER"
    assert event["res"]["order_id"] == "0388X"


def test_token_routes_are_never_stored(sdk):
    # webull_io itself parses token replies (PENDING check); the recorder must keep none of it
    reply = _Response({"access_token": "tok-should-not-appear", "status": "NORMAL"})
    with tick_runtime.tick_scope(TICK):
        sdk(_Request("/openapi/auth/token/check", "POST"), reply=reply)
        events = fr.snapshot()["events"]
    event = next(e for e in events if e["k"] == "wb")
    assert event["st"] == 200 and "res" not in event and "sha" not in event
    assert "tok-should-not-appear" not in json.dumps(events)


def test_a_recorder_failure_never_changes_what_the_broker_call_returns_or_raises(sdk, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(fr, "webull_exchange", explode)
    monkeypatch.setattr(fr, "checkpoint", explode)
    reply = _Response({"status": "PENDING"})
    boom = RuntimeError("broker down")
    with tick_runtime.tick_scope(TICK):
        assert sdk(_Request("/trading/orders/get"), reply=reply) is reply
        with pytest.raises(RuntimeError) as caught:
            sdk(_Request("/trading/orders/place", "POST"), error=boom)
    assert caught.value is boom
