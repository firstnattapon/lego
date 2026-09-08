"""Deterministic local performance probe; prints JSON and never touches cloud."""
import json
import platform
import sys
import statistics
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lego_one_row import Config, build_decision


def percentile(values, p):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * p)))
    return ordered[index]


def main():
    cfg = Config(
        "AAPL", 1500, 25, dna_code="bypass:100",
        strategy_id="shannon_demon_lego_v2", quantity_increment=1,
    )
    samples_ms = []
    outputs = []
    for i in range(1000):
        price = 90.0 + (i % 200) / 10.0
        holdings = float(i % 18)
        started = time.perf_counter_ns()
        decision = build_decision(cfg, price, holdings, i % 2)
        samples_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
        outputs.append({
            "status": decision.status,
            "side": decision.side,
            "quantity": decision.quantity,
        })
    hot_bytes = len(json.dumps({
        "config": cfg.__dict__, "last_decisions": outputs[-32:],
        "active_intent": None,
    }, sort_keys=True, default=str).encode())
    print(json.dumps({
        "workload": "1000 deterministic pure build_decision evaluations",
        "samples": len(samples_ms),
        "python": platform.python_version(),
        "p50_ms": percentile(samples_ms, 0.50),
        "p95_ms": percentile(samples_ms, 0.95),
        "max_ms": max(samples_ms),
        "mean_ms": statistics.fmean(samples_ms),
        "hot_state_probe_bytes": hot_bytes,
        "budgets": {"pure_engine_p95_ms": 50, "hot_state_target_bytes": 65536},
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
