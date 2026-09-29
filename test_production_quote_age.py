"""Production must never inherit the sandbox's delayed-feed allowance."""
from datetime import timedelta
import pytest
import execution_service as execution
from test_dispatch_overshoot import CFG, NOW, dispatch_fixture, isolate, snapshot


@pytest.mark.parametrize("environment,expected,places", [
    ("UAT", "SUBMITTED", 1), ("PROD", "SUPPRESSED_STATE_CHANGED", 0)])
def test_five_minute_quote_has_environment_specific_gate(monkeypatch, environment, expected, places):
    runtime, intent, claim, client, _ = dispatch_fixture(monkeypatch, environment=environment)
    stale = (NOW - timedelta(seconds=300)).isoformat()
    monkeypatch.setattr(execution, "fetch_snapshot", lambda *_: {**snapshot(), "quote_time": stale})
    result = execution._dispatch_or_reconcile_one(client, object(), CFG, intent, claim, runtime)
    assert result["status"] == expected
    assert len(client.order_v3.place_order.calls) == places
