"""Offline daily reconciliation; snapshots cannot certify a live release.

Private broker schema: see docs/CONTINUOUS_RELEASE_V4_TH.md.
Only hashes/counts/checks are output. Account/order data stays private.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lego_orders import normalize_status, TERMINAL_STATUSES


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite evidence")
    return result


def totals(order):
    qty, fee = number(order["filled_quantity"]), number(order["filled_fee"])
    price = number(order["filled_price"]) if qty else Decimal(0)
    if qty < 0 or fee < 0 or (qty and price <= 0) or order["side"] not in {"BUY", "SELL"}:
        raise ValueError("invalid execution")
    return qty, qty * price, fee, (1 if order["side"] == "SELL" else -1) * qty * price - fee


def snapshot(snapshot):
    if (snapshot.get("orders_complete") is not True or snapshot.get("positions_complete") is not True
            or snapshot.get("environment") not in {"UAT", "PROD"} or not snapshot.get("account_fingerprint")):
        raise ValueError("incomplete snapshot")
    at = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00"))
    if at.tzinfo is None or at > datetime.now(timezone.utc):
        raise ValueError("invalid snapshot time")
    orders = snapshot["orders"]
    by_id = {o["client_order_id"]: o for o in orders}
    if len(by_id) != len(orders) or not all(by_id):
        raise ValueError("duplicate/missing identity")
    for order in orders:
        if not order.get("symbol"): raise ValueError("missing symbol")
        totals(order)
    number(snapshot["cash"])
    for value in snapshot["positions"].values(): number(value)
    return at, by_id


def balances_match(broker):
    end, orders = snapshot(broker)
    start, prior = snapshot(broker["opening"])
    opening = broker["opening"]
    if (not 0 < (end - start).total_seconds() <= 48 * 3600 or prior.keys() - orders.keys()
            or any(opening[k] != broker[k] for k in ("environment", "account_fingerprint"))):
        raise ValueError("opening snapshot does not cover this day/account")
    positions, cash = defaultdict(Decimal), number(broker["external_cash_delta"])
    for run, order in orders.items():
        qty, notional, fee, amount = totals(order)
        before = prior.get(run)
        pq = pc = Decimal(0)
        if before:
            pq, pn, pf, pc = totals(before)
            if (any(before[k] != order[k] for k in ("symbol", "side"))
                    or qty < pq or notional < pn or fee < pf):
                raise ValueError("cumulative evidence regressed")
        positions[order["symbol"]] += (1 if order["side"] == "BUY" else -1) * (qty - pq)
        cash += amount - pc
    # Explicit zero adjustments required; never silently assume no transfers.
    for symbol, delta in broker["external_position_delta"].items(): positions[symbol] += number(delta)
    symbols = set(opening["positions"]) | set(broker["positions"]) | set(positions)
    return (abs(number(broker["cash"]) - number(opening["cash"]) - cash) <= Decimal("0.01")
            and all(abs(number(broker["positions"].get(s, 0)) - number(opening["positions"].get(s, 0))
                        - positions[s]) <= Decimal("0.000001") for s in symbols))


def build(export, broker=None):
    issues, pending, intents = [], [], {}
    for path in ("webull_lego_order_outbox_archive", "webull_lego_order_outbox"):
        for chain, records in export.get(path, {}).items():
            for run, intent in records.items():
                if run in intents and intents[run] != (chain, intent): issues.append("duplicate_local_identity")
                intents[run] = (chain, intent)
    attempted = {r: item for r, item in intents.items() if item[1].get("place_attempted")}
    checks = {k: "BLOCKED" for k in ("orders_fills_fees", "broker_cashflow_ledger", "model_realized_witnesses", "positions_and_cash")}
    matched = 0
    if broker is not None:
        try:
            _, orders = snapshot(broker)
            failures = {k: [] for k in checks}
            if orders.keys() - attempted.keys(): failures["orders_fills_fees"].append("broker_order_without_attempt_witness")
            for run, (chain, intent) in attempted.items():
                order = orders.get(run)
                if order is None:
                    failures["orders_fills_fees"].append("attempt_missing_from_broker_snapshot")
                    continue
                if (intent.get("runtime_identity_fingerprint") != broker["account_fingerprint"]
                        or order["symbol"] != intent["symbol"] or order["side"] != intent["side"]):
                    failures["orders_fills_fees"].append("account_or_order_identity_mismatch")
                    continue
                qty, notional, fee, cash = totals(order)
                status = normalize_status(order["status"])
                if (status != normalize_status(intent.get("broker_status") or intent["status"])
                        or qty != number(intent["filled_quantity"]) or fee != number(intent["filled_fee"])
                        or (qty and number(order["filled_price"]) != number(intent["filled_price"]))):
                    failures["orders_fills_fees"].append("broker_outbox_mismatch")
                else: matched += 1
                if status not in TERMINAL_STATUSES: pending.append("nonterminal_order")
                event = export.get("webull_lego_broker_cashflow", {}).get(chain, {}).get("events", {}).get(run)
                if qty or fee:
                    if not event or (number(event.get("cumulative_quantity")) != qty
                            or number(event.get("cumulative_notional")) != notional
                            or number(event.get("actual_fees")) != fee
                            or number(event.get("cash_cumulative")) != cash or event.get("side") != order["side"]):
                        failures["broker_cashflow_ledger"].append("broker_cashflow_ledger_mismatch")
                elif event and number(event.get("cash_cumulative")) != 0:
                    failures["broker_cashflow_ledger"].append("zero_fill_has_cashflow")
                if qty and status in TERMINAL_STATUSES:
                    flow = export.get("webull_lego_state", {}).get(chain, {}).get("execution_cashflow", {})
                    final = flow.get("finalized_runs", {}).get(run)
                    realized = (export.get("webull_lego_realized", {}).get(chain, {}).get("applied_fills", {}).get(run)
                                or export.get("webull_lego_realized_events", {}).get(chain, {}).get(run))
                    if (intent.get("cashflow_finalized") is not True or not final or not realized
                            or number(final.get("filled_quantity")) != qty
                            or number(final.get("filled_price")) != number(order["filled_price"])
                            or number(realized.get("quantity")) != qty
                            or number(realized.get("average_price")) != number(order["filled_price"])
                            or number(realized.get("fee")) != fee or realized.get("side") != order["side"]):
                        failures["model_realized_witnesses"].append("model_realized_witness_mismatch")
            for key in checks:
                if key == "positions_and_cash": continue
                checks[key] = "FAIL" if failures[key] else "PASS"
                issues.extend(failures[key])
            if "opening" in broker:
                equal = balances_match(broker)
                checks["positions_and_cash"] = "PASS" if equal else "FAIL"
                if not equal: issues.append("positions_or_cash_delta_mismatch")
        except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
            issues.append("broker_evidence_incomplete_or_invalid")
    fences = sum(bool(d.get("inflight_run_id")) for d in export.get("webull_lego_order_dispatch_locks", {}).values())
    audit_pending = sum(bool(i.get("transition_pending") or i.get("audit_pending")) for _, i in intents.values())
    checks["fences_and_audit"] = "BLOCKED" if fences or audit_pending else "PASS"
    status = "FAIL" if issues else "BLOCKED" if pending or "BLOCKED" in checks.values() else "PASS"
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "status": status,
            "scope": "supplied daily snapshots only", "real_money_ready": False,
            "statuses": dict(Counter(i.get("status", "UNKNOWN") for _, i in intents.values())),
            "attempts": len(attempted), "broker_orders_matched": matched,
            "unresolved_fences": fences, "audit_pending": audit_pending,
            "issues": dict(Counter(issues)), "pending": dict(Counter(pending)), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--broker-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() in {p.resolve() for p in (args.export, args.broker_evidence) if p}:
        parser.error("output must not overwrite source evidence")
    raw = args.export.read_bytes()
    broker_raw = args.broker_evidence.read_bytes() if args.broker_evidence else None
    report = build(json.loads(raw), json.loads(broker_raw) if broker_raw else None)
    report["source_sha256"] = hashlib.sha256(raw).hexdigest()
    if broker_raw: report["broker_evidence_sha256"] = hashlib.sha256(broker_raw).hexdigest()
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output: args.output.write_text(rendered, encoding="utf-8")
    else: print(rendered)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
