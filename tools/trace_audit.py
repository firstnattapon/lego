#!/usr/bin/env python3
"""Offline, read-only digest of a LEGO run: what happened, what is wrong, and what is still unknown.

It exists so the next audit starts from one command instead of ten ad hoc scripts, and so an
AI (or a person) reading the flight recorder sees the flow the way the system took it.

  python -m tools.trace_audit --logs cloud-logs.json --rtdb rtdb-export.json --yaml service.yaml \
         --repo . [--since 2026-10-08T00:00:00Z] [--run <run id prefix>] [--json] [--fail-on P0]
  python -m tools.trace_audit --live [--follow] [--chain UBER_xxx]     # needs Firebase credentials

Every input is optional; a section says what it could not check instead of guessing. Nothing
is written, nothing calls a broker. Standard library only, so it runs on any machine that has the
exports. Order and run ids are shortened to 8 characters; account ids and secrets are never read
into the output. The decision formulas mirror lego_one_row.py and test_trace_audit.py pins them
to the production code, so a formula change cannot silently make this tool wrong.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

UTC = timezone.utc

# The 17-column row contract. These are the only Thai strings in this tool; the parity test
# pins them to lego_one_row.COLUMN_ORDER.
COL = {
    "time": "เวลา (UTC)", "symbol": "สินทรัพย์", "status": "สถานะ", "step": "DNA step",
    "signal": "DNA signal", "price": "ราคา Pₙ (USD)", "holdings": "จำนวนถือครอง (หุ้น)",
    "action": "คำสั่ง", "side": "ฝั่ง", "reason": "เหตุผล", "qty": "จำนวนสั่ง (หุ้น)",
    "value": "มูลค่าพอร์ต (USD)", "gap": "ส่วนต่างเป้าหมาย (USD)", "R": "Rₙ อ้างอิง (USD)",
    "dA": "ΔAₙ ต่อสเต็ป (USD)", "A": "Aₙ สะสม (USD)", "E": "Eₙ ส่วนเกินสะสม (USD)",
}
DECISION_STATUSES = frozenset({"SNAPSHOT_READY", "PASS_DNA_ZERO", "PASS_THRESHOLD",
                               "PASS_MIN_ORDER", "READY_BUY", "READY_SELL"})
TERMINAL_INTENT = frozenset({
    "FILLED", "CANCELLED", "CANCELED", "REJECTED", "FAILED", "EXPIRED", "EXPIRED_UNSENT",
    "SUPPRESSED_ACTIVE_ORDER", "SUPPRESSED_STATE_CHANGED", "NOT_PLACED"})
LEVELS = ("P0", "P1", "P2", "INFO")
IN_FLIGHT_MINUTES = 30                     # an order older than this and not terminal is stuck, not slow
TICK_BUDGET_SECONDS = 45.0               # the Cloud Run request timeout the service is deployed with


# ------------------------------------------------------------------------------ small helpers

_FRACTION = re.compile(r"\.(\d{6})\d+")


def parse_ts(value):
    """ISO-8601 (Z or offset, up to nanoseconds) -> aware UTC datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r".\1", value.strip())
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def iso(moment):
    return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(value):
    """float for numbers and numeric strings (RTDB mixes both); None for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def whole(value):
    """12.0 -> 12 for counters and ids that RTDB may hand back as floats."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def close(a, b, rel=1e-9, absolute=1e-9):
    if a is None or b is None:
        return False
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


def short(value, n=8):
    text = "" if value is None else str(value)
    return text[:n]


def quantile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def spread(values, digits=1):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "p50": round(quantile(values, 0.5), digits),
            "p95": round(quantile(values, 0.95), digits), "max": round(max(values), digits)}


def counted(items, limit=None):
    return dict(Counter(items).most_common(limit))


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------- the decision equations

def _dec(value):
    return Decimal(str(value))


def recompute_decision(i: dict) -> dict:
    """The decision of one slot from its recorded inputs (mirror of lego_one_row.build_decision).

    ``i``: P_n, holdings, FIX_C, DIFF, signal, and optionally P0 (None = genesis), dp, inc,
    strategy. Returns V_n, gap, R_n, status, side, qty.
    """
    price, holdings = float(i["P_n"]), float(i["holdings"])
    fix_c, diff = float(i["FIX_C"]), float(i["DIFF"])
    value = holdings * price
    gap = value - fix_c
    p0 = i.get("P0")
    reference = 0.0 if not p0 else fix_c * math.log(price / float(p0))
    v2 = str(i.get("strategy") or "").endswith("_v2")
    dp = int(i.get("dp", 5))
    inc = float(i.get("inc") or 1.0)
    qty = 0.0
    if int(i["signal"]) == 0:
        status = "PASS_DNA_ZERO"
    elif abs(gap) <= diff:
        status = "PASS_THRESHOLD"
    else:
        if v2:
            raw = _dec(abs(gap)) / _dec(price)
            qty = float((raw / _dec(inc)).to_integral_value(rounding=ROUND_DOWN) * _dec(inc))
        else:
            qty = round(abs(gap) / price, dp)
        if gap > diff:                                   # a SELL never exceeds what is held
            ceiling = _dec(holdings).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_DOWN)
            qty = min(qty, float(ceiling))
        if v2 and 0 < qty < 1.0 and _dec(qty) * _dec(price) < Decimal("1.0"):
            qty = 0.0
        if qty <= 0:
            status, qty = ("PASS_MIN_ORDER" if v2 else "PASS_THRESHOLD"), 0.0
        else:
            status = "READY_SELL" if gap > diff else "READY_BUY"
    side = {"READY_SELL": "SELL", "READY_BUY": "BUY"}.get(status, "")
    return {"V_n": value, "gap": gap, "R_n": reference, "status": status, "side": side, "qty": qty}


def recompute_fill(fix_c, p_fill, p_acted, a_prev, reference):
    """The fill-time recurrence: dA = FIX_C*(P_fill/P_acted - 1), A = A_prev + dA, E = A - R."""
    delta = fix_c * (p_fill / p_acted - 1.0)
    cumulative = a_prev + delta
    return {"delta_actual": delta, "actual_cumulative": cumulative, "excess": cumulative - reference}


def _mismatch(bucket, limit, **fields):
    bucket["count"] += 1
    if len(bucket["items"]) < limit:
        bucket["items"].append(fields)


def _bucket():
    return {"count": 0, "items": []}


def check_rows(rows, *, fix_c=None, diff=None, p0=None, limit=20) -> dict:
    """Recompute every committed 17-column row from its own inputs."""
    rows = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: num(r.get("version")) or 0)
    result = {"n": len(rows), "fix_c": fix_c, "diff": {"given": diff}, "p0": p0, "rules": {},
              "gated": {}, "mismatch": _bucket()}
    if not rows:
        return result
    c = COL
    implied = [v - g for r in rows for v, g in [(num(r.get(c["value"])), num(r.get(c["gap"])))]
               if v is not None and g is not None]
    if fix_c is None and implied:
        fix_c = statistics.median(implied)
    result["fix_c"] = fix_c
    if fix_c is None:
        result["note"] = "no value/gap columns; nothing to recompute"
        return result
    # DIFF is a config value; when it is not given, bound it from the rows' own decisions.
    below = [abs(num(r[c["gap"]])) for r in rows if r.get(c["status"]) == "PASS_THRESHOLD"
             and num(r.get(c["gap"])) is not None]
    above = [abs(num(r[c["gap"]])) for r in rows if str(r.get(c["status"])).startswith("READY")
             and num(r.get(c["gap"])) is not None]
    lo, hi = (max(below) if below else 0.0), (min(above) if above else math.inf)
    result["diff"].update(lower=lo, upper=None if hi is math.inf else hi)
    if diff is None:
        used = lo
        result["diff"]["source"] = "inferred from the rows"
    else:
        used = diff
        result["diff"]["source"] = "config"
        if not (lo <= diff < hi):
            _mismatch(result["mismatch"], limit, version=None, field="diff_vs_rows",
                      recorded=diff, expected=f"[{lo:.4f}, {hi:.4f})")
    result["diff"]["used"] = used
    if p0 is None:
        result["note"] = "P0 unknown: the R column was not checked"
    rules = Counter()
    for row in rows:
        version = row.get("version")
        price, holdings = num(row.get(c["price"])), num(row.get(c["holdings"]))
        value, gap = num(row.get(c["value"])), num(row.get(c["gap"]))
        if None in (price, holdings, value, gap) or num(row.get(c["signal"])) is None:
            _mismatch(result["mismatch"], limit, version=version, field="inputs",
                      recorded=None, expected="price, holdings, value, gap, signal")
            continue
        if not close(value - gap, fix_c, 1e-7, 1e-7):
            _mismatch(result["mismatch"], limit, version=version, field="FIX_C", recorded=value - gap, expected=fix_c)
        base = {"P_n": price, "holdings": holdings, "FIX_C": fix_c, "DIFF": used,
                "signal": row.get(c["signal"]), "P0": p0}
        status = row.get(c["status"])
        quantity = num(row.get(c["qty"])) or 0.0
        expected = recompute_decision({**base, "strategy": "x_v2", "dp": 2, "inc": 0.01})
        for field, column in (("V_n", "value"), ("gap", "gap")):
            if not close(expected[field], num(row.get(c[column])), 1e-9, 1e-7):
                _mismatch(result["mismatch"], limit, version=version, field=field,
                          recorded=num(row.get(c[column])), expected=expected[field])
        if p0 and not close(expected["R_n"], num(row.get(c["R"])), 1e-9, 1e-6):
            _mismatch(result["mismatch"], limit, version=version, field="R_n",
                      recorded=num(row.get(c["R"])), expected=expected["R_n"])
        if row.get("cashflow_status") != "FINALIZED" and (num(row.get(c["dA"])) or 0.0) != 0.0:
            _mismatch(result["mismatch"], limit, version=version, field="dA_before_fill",
                      recorded=row.get(c["dA"]), expected=0)
        if status not in DECISION_STATUSES:                 # a gate (e.g. recovery) replaced the decision
            result["gated"][str(status)] = result["gated"].get(str(status), 0) + 1
            if quantity != 0.0:
                _mismatch(result["mismatch"], limit, version=version, field="gated_qty",
                          recorded=quantity, expected=0)
            continue
        increment = _increment(row)
        candidates = {"floor_v2": recompute_decision({**base, "strategy": "x_v2", "dp": 2, "inc": increment}),
                      "round_legacy": recompute_decision({**base, "strategy": "x", "dp": 2, "inc": increment})}
        matched = [name for name, out in candidates.items()
                   if out["status"] == status and close(out["qty"], quantity, 1e-9, 1e-9)
                   and out["side"] == (row.get(c["side"]) or "")]
        if matched:
            rules[matched[0]] += 1
        else:
            reference_out = candidates["floor_v2"]
            _mismatch(result["mismatch"], limit, version=version, field="decision",
                      recorded=f"{status} {row.get(c['side']) or '-'} {quantity}",
                      expected=f"{reference_out['status']} {reference_out['side'] or '-'} {reference_out['qty']}")
    result["rules"] = dict(rules)
    versions = [whole(num(r.get("version"))) for r in rows]
    steps = [whole(num(r.get(c["step"]))) for r in rows]
    result["versions"] = {"first": versions[0], "last": versions[-1],
                          "contiguous": all(b - a == 1 for a, b in zip(versions, versions[1:]))}
    result["steps"] = {"first": steps[0], "last": steps[-1],
                       "gaps": [(a, b) for a, b in zip(steps, steps[1:]) if b - a != 1][:10],
                       "increasing": all(b > a for a, b in zip(steps, steps[1:]))}
    result["uncommitted"] = sum(1 for r in rows if r.get("committed") is not True)
    result["cashflow_status"] = counted(r.get("cashflow_status") for r in rows)
    result["status_counts"] = counted(r.get(c["status"]) for r in rows)
    return result


def _increment(row):
    capability = row.get("instrument_capability")
    if isinstance(capability, dict):
        value = num(capability.get("quantity_increment"))
        if value and value > 0:
            return value
    return 1.0


# ------------------------------------------------------------------------------ the money ledger

def check_fills(state, rows, intents, realized, *, fix_c, p0, limit=20) -> dict:
    """ΔA/A/E of every finalized fill, the chain between fills, and the position walk."""
    cash = (state or {}).get("execution_cashflow") or {}
    finalized = cash.get("finalized_runs") or {}
    applied = (realized or {}).get("applied_fills") or {}
    fills = sorted(((rid, w) for rid, w in finalized.items() if isinstance(w, dict)),
                   key=lambda kv: num(kv[1].get("seq")) or 0)
    result = {"n": len(fills), "mismatch": _bucket(), "fills": [],
              "finalized_seq": cash.get("finalized_seq"), "applied_seq": (realized or {}).get("applied_seq")}
    by_run = {r.get("run_id"): r for r in rows if isinstance(r, dict)}
    previous = None
    for run_id, w in fills:
        seq = whole(num(w.get("seq")))
        p_fill, p_acted = num(w.get("filled_price")), num(w.get("previous_action_price"))
        a_prev, reference = num(w.get("previous_actual_cumulative")), num(w.get("reference"))
        entry = {"seq": seq, "run": short(run_id), "at": w.get("at")}
        result["fills"].append(entry)
        if None in (fix_c, p_fill, p_acted, a_prev, reference) or not p_acted:
            _mismatch(result["mismatch"], limit, seq=seq, field="inputs", recorded=None, expected="complete witness")
            previous = w
            continue
        expect = recompute_fill(fix_c, p_fill, p_acted, a_prev, reference)
        for field in ("delta_actual", "actual_cumulative", "excess"):
            if not close(expect[field], num(w.get(field)), 1e-9, 1e-7):
                _mismatch(result["mismatch"], limit, seq=seq, field=field,
                          recorded=num(w.get(field)), expected=expect[field])
        want_a = num(previous.get("actual_cumulative")) if previous else 0.0
        want_p = num(previous.get("filled_price")) if previous else p0
        if not close(a_prev, want_a, 1e-9, 1e-7):
            _mismatch(result["mismatch"], limit, seq=seq, field="chain_A", recorded=a_prev, expected=want_a)
        if want_p is not None and not close(p_acted, want_p, 1e-9, 1e-9):
            _mismatch(result["mismatch"], limit, seq=seq, field="chain_P_acted", recorded=p_acted, expected=want_p)
        row = by_run.get(run_id)
        if row is not None and p0 and num(row.get(COL["price"])):
            decision_reference = fix_c * math.log(num(row[COL["price"]]) / p0)
            entry["reference_gap"] = round(decision_reference - reference, 9)
        # position walk: the position moved by exactly what the fill says
        side = str((applied.get(run_id) or {}).get("side") or (intents.get(run_id) or {}).get("side") or "").upper()
        after, quantity = num(w.get("holdings_after")), num(w.get("filled_quantity"))
        before = num(previous.get("holdings_after")) if previous else num((intents.get(run_id) or {}).get("decision_holdings"))
        sign = {"BUY": 1.0, "SELL": -1.0}.get(side)
        if None not in (after, quantity, before) and sign is not None:
            if not close(after - before, sign * quantity, 1e-9, 1e-6):
                _mismatch(result["mismatch"], limit, seq=seq, field="position_walk",
                          recorded=after - before, expected=sign * quantity)
        previous = w
    if fills:
        last = fills[-1][1]
        for field, key in (("actual_cumulative", "actual_cumulative"), ("excess", "excess")):
            if not close(num(last.get(field)), num(cash.get(key)), 1e-9, 1e-7):
                _mismatch(result["mismatch"], limit, seq=num(last.get("seq")), field="state_" + key,
                          recorded=num(cash.get(key)), expected=num(last.get(field)))
        if not close(num(cash.get("last_action_price")), num(last.get("filled_price")), 1e-9, 1e-9):
            _mismatch(result["mismatch"], limit, seq=num(last.get("seq")), field="state_last_action_price",
                      recorded=num(cash.get("last_action_price")), expected=num(last.get("filled_price")))
    return result


def check_ledger(state, realized, fills_report) -> dict:
    """Exactly-once: contiguous seq, finalized == applied, cumulative realized adds up."""
    applied = (realized or {}).get("applied_fills") or {}
    items = sorted((v for v in applied.values() if isinstance(v, dict)), key=lambda v: num(v.get("seq")) or 0)
    seqs = [num(v.get("seq")) for v in items]
    problems = []
    if seqs and seqs != [float(i) for i in range(1, len(seqs) + 1)]:
        problems.append("applied seq not contiguous from 1")
    if (realized or {}).get("applied_seq") is not None and items and num(realized["applied_seq"]) != len(items):
        problems.append("applied_seq != number of applied fills")
    finalized_seq = ((state or {}).get("execution_cashflow") or {}).get("finalized_seq")
    if finalized_seq is not None and items and num(finalized_seq) != len(items):
        problems.append("finalized_seq != applied fills")
    total = sum(num(v.get("realized_delta")) or 0.0 for v in items)
    if items and not close(total, num((realized or {}).get("cumulative_realized")), 1e-9, 1e-7):
        problems.append("sum(realized_delta) != cumulative_realized")
    running = 0.0
    for v in items:
        running += num(v.get("realized_delta")) or 0.0
        if num(v.get("cumulative_realized_after")) is not None and not close(running, num(v["cumulative_realized_after"]), 1e-9, 1e-7):
            problems.append(f"cumulative chain breaks at seq {v.get('seq')}")
            break
    net = sum({"BUY": 1.0, "SELL": -1.0}.get(str(v.get("side")).upper(), 0.0) * (num(v.get("quantity")) or 0.0)
              for v in items)
    return {"applied": len(items), "finalized_seq": finalized_seq, "net_position_change": round(net, 6),
            "cumulative_realized": num((realized or {}).get("cumulative_realized")), "problems": problems}


def economics(realized) -> dict:
    applied = (realized or {}).get("applied_fills") or {}
    items = [v for v in applied.values() if isinstance(v, dict)]
    out = []
    for v in items:
        quantity, price, fee = num(v.get("quantity")), num(v.get("average_price")), num(v.get("fee"))
        if None in (quantity, price, fee) or quantity * price <= 0:
            continue
        out.append({"seq": v.get("seq"), "side": v.get("side"), "notional": quantity * price,
                    "fee": fee, "fee_pct": 100.0 * fee / (quantity * price)})
    if not out:
        return {"n": 0}
    pcts = [o["fee_pct"] for o in out]
    median = statistics.median(pcts)
    return {"n": len(out), "fees_total": round(sum(o["fee"] for o in out), 6),
            "notional_total": round(sum(o["notional"] for o in out), 4),
            "fee_pct": {"min": round(min(pcts), 4), "median": round(median, 4), "max": round(max(pcts), 4)},
            "round_trip_fee_pct": round(2 * median, 4),
            "cumulative_realized": num((realized or {}).get("cumulative_realized")),
            "note": "realized_delta nets the fees of both legs, so cumulative_realized is already after fees"}


# --------------------------------------------------------------------------------- Cloud Logging

def analyze_logs(entries, *, since=None) -> dict:
    ticks, ops, http_rows, texts = [], [], [], []
    instances, revisions = Counter(), Counter()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
        stamp = parse_ts(payload.get("timestamp")) or parse_ts(entry.get("timestamp"))
        if since and (stamp is None or stamp < since):
            continue
        labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
        resource = (entry.get("resource") or {}).get("labels") or {}
        if labels.get("instanceId"):
            instances[short(labels["instanceId"], 12)] += 1
        if resource.get("revision_name"):
            revisions[resource["revision_name"]] += 1
        event = payload.get("event")
        if isinstance(entry.get("textPayload"), str):
            texts.append((stamp, entry["textPayload"]))
        if event == "lego_tick_completed":
            ticks.append({**payload, "_at": stamp, "_severity": payload.get("severity") or entry.get("severity")})
        elif event == "lego_operation":
            ops.append({**payload, "_at": stamp})
        elif isinstance(entry.get("httpRequest"), dict):
            latency = str(entry["httpRequest"].get("latency") or "").rstrip("s")
            http_rows.append({"status": entry["httpRequest"].get("status"), "latency": num(latency), "_at": stamp})
    ticks.sort(key=lambda t: t["_at"] or datetime.min.replace(tzinfo=UTC))
    result = {"entries": len(entries) if isinstance(entries, list) else 0, "ticks": {"n": len(ticks)},
              "http": {"n": len(http_rows)}, "instances": dict(instances), "revisions": dict(revisions)}
    if ticks:
        stamps = [t["_at"] for t in ticks if t["_at"]]
        intervals = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
        typical = statistics.median(intervals) if intervals else None
        limit = max(180.0, 3 * typical) if typical else 180.0
        gaps = [{"from": iso(a), "to": iso(b), "seconds": round((b - a).total_seconds())}
                for a, b in zip(stamps, stamps[1:]) if (b - a).total_seconds() > limit]
        result["ticks"].update(
            first=iso(stamps[0]) if stamps else None, last=iso(stamps[-1]) if stamps else None,
            interval_s=None if typical is None else round(typical, 1),
            by_severity=counted(t["_severity"] for t in ticks),
            by_http=counted(t.get("http_status") for t in ticks),
            by_pipeline=counted(t.get("pipeline_status") for t in ticks),
            by_business=counted(t.get("business_status") for t in ticks),
            duration_ms=spread([num(t.get("duration_ms")) for t in ticks]),
            slowest=[{"at": iso(t["_at"]), "tick": short(t.get("correlation_id")), "ms": num(t.get("duration_ms")),
                      "business": t.get("business_status")}
                     for t in sorted(ticks, key=lambda t: -(num(t.get("duration_ms")) or 0))[:5]],
            near_budget=sum(1 for t in ticks if (num(t.get("duration_ms")) or 0) > 0.7 * TICK_BUDGET_SECONDS * 1000),
            gaps=gaps[:10], gap_count=len(gaps),
            segments=_segments(ticks), error_runs=_error_runs(ticks),
            halt_since=next((t["halt_since"] for t in reversed(ticks) if t.get("halt_since")), None),
            paused=sum(1 for t in ticks if t.get("reconciliation_paused")),
            environment=counted(t.get("environment") for t in ticks if t.get("environment")),
            mode=counted(t.get("mode") for t in ticks if t.get("mode")))
    if http_rows:
        result["http"].update(by_status=counted(r["status"] for r in http_rows),
                              latency_s=spread([r["latency"] for r in http_rows], 2))
    result["webull"] = _webull_ops(ops, ticks)
    result["webull"]["sdk_errors"] = _sdk_text_errors(texts)
    return result


def _segments(ticks):
    """Consecutive ticks with the same business status, as time ranges."""
    out = []
    for t in ticks:
        status = t.get("business_status")
        if out and out[-1]["status"] == status:
            out[-1]["to"], out[-1]["n"] = iso(t["_at"]), out[-1]["n"] + 1
        else:
            out.append({"status": status, "from": iso(t["_at"]), "to": iso(t["_at"]), "n": 1})
    return out


def _error_runs(ticks):
    runs, current = [], None
    for t in ticks:
        bad = t["_severity"] == "ERROR" or (num(t.get("http_status")) or 200) >= 500
        if bad:
            kinds = [e.get("type") for e in (t.get("errors") or []) if isinstance(e, dict)]
            if current is None:
                current = {"from": iso(t["_at"]), "to": iso(t["_at"]), "n": 0, "pipeline": Counter(), "types": Counter()}
            current["to"], current["n"] = iso(t["_at"]), current["n"] + 1
            current["pipeline"][t.get("pipeline_status")] += 1
            current["types"].update(k for k in kinds if k)
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)
    return [{**r, "pipeline": dict(r["pipeline"].most_common(3)), "types": dict(r["types"].most_common(3))}
            for r in runs]


_SDK_EXCEPTION = re.compile(r"get_response exception\.\s*(\{.*?\})", re.S)
_SDK_ACTION = re.compile(r'"_action_name":\s*"([^"]+)"')


def _sdk_text_errors(texts) -> list[dict]:
    """The Webull SDK logs its own failures as plain text (code, HTTP status, request id).

    Revisions before the flight recorder have nothing else, so read those lines too. The route
    comes from the request dump the SDK prints just before (same second)."""
    found, last_route = {}, (None, None)
    for stamp, text in sorted(((t, x) for t, x in texts if t is not None), key=lambda item: item[0]):
        route = _SDK_ACTION.search(text)
        if route:
            last_route = (stamp, route.group(1))
        match = _SDK_EXCEPTION.search(text)
        if not match:
            continue
        try:
            detail = json.loads(match.group(1))
        except ValueError:
            continue
        near = last_route[0] is not None and abs((stamp - last_route[0]).total_seconds()) <= 2
        key = (last_route[1] if near else None, detail.get("error_code"), detail.get("http_status"))
        slot = found.setdefault(key, {"n": 0, "first": iso(stamp), "last": iso(stamp), "request_ids": []})
        slot["n"] += 1
        slot["last"] = iso(stamp)
        rid = detail.get("request_id")
        if rid and rid not in slot["request_ids"] and len(slot["request_ids"]) < 3:
            slot["request_ids"].append(rid)
    return [{"route": k[0], "code": k[1], "http": k[2], **v} for k, v in sorted(found.items(), key=lambda kv: -kv[1]["n"])]


def _webull_ops(ops, ticks):
    table = {}
    for o in ops:
        name = str(o.get("operation") or o.get("phase") or "?")
        row = table.setdefault(name, {"n": 0, "errors": 0, "ms": [], "codes": Counter(), "http": Counter(),
                                      "request_ids": []})
        row["n"] += 1
        row["ms"].append(num(o.get("duration_ms")))
        if o.get("outcome") == "error":
            row["errors"] += 1
        if o.get("error_code"):
            row["codes"][o["error_code"]] += 1
        if o.get("http_status") and (o.get("outcome") == "error" or num(o["http_status"]) >= 400):
            row["http"][o["http_status"]] += 1
        if o.get("request_id") and o.get("outcome") == "error" and o["request_id"] not in row["request_ids"] \
                and len(row["request_ids"]) < 3:
            row["request_ids"].append(o["request_id"])
    operations = {name: {"n": r["n"], "errors": r["errors"], "ms": spread(r["ms"]),
                         "codes": dict(r["codes"]), "http": dict(r["http"]), "request_ids": r["request_ids"]}
                  for name, r in sorted(table.items(), key=lambda kv: -kv[1]["n"])}
    broker = {}
    for t in ticks:
        for e in t.get("errors") or []:
            detail = e.get("broker_error") if isinstance(e, dict) else None
            if not isinstance(detail, dict):
                continue
            key = (detail.get("operation"), detail.get("code"), detail.get("http_status"))
            slot = broker.setdefault(key, {"n": 0, "first": iso(t["_at"]), "last": iso(t["_at"]), "request_ids": []})
            slot["n"] += 1
            slot["last"] = iso(t["_at"])
            if detail.get("request_id") and detail["request_id"] not in slot["request_ids"] \
                    and len(slot["request_ids"]) < 3:
                slot["request_ids"].append(detail["request_id"])
    return {"operations": operations,
            "errors": [{"op": k[0], "code": k[1], "http": k[2], **v}
                       for k, v in sorted(broker.items(), key=lambda kv: -kv[1]["n"])]}


# ----------------------------------------------------------------------------- RTDB: the orders

def _intent_view(doc, as_of):
    created = parse_ts(doc.get("created_at"))
    blockers = doc.get("expiry_proof_blockers")
    policy = doc.get("cancel_policy") if isinstance(doc.get("cancel_policy"), dict) else {}
    return {
        "run": short(doc.get("run_id") or doc.get("client_order_id")),
        "status": doc.get("status"), "side": doc.get("side") or doc.get("intent_side"),
        "qty": doc.get("quantity") or doc.get("intent_quantity"),
        "broker_status": doc.get("broker_status"), "broker_id": short(doc.get("broker_order_id")),
        "created_at": doc.get("created_at"), "placed_at": doc.get("placed_at"),
        "filled_quantity": doc.get("filled_quantity"), "cancel_attempts": doc.get("cancel_attempt_count"),
        "cancel_refused_at": doc.get("cancel_refused_at"), "cancel_error": doc.get("cancel_last_error_code"),
        "hold_deadline": doc.get("cancel_confirmation_deadline"),
        "needs_manual": bool(doc.get("needs_manual_check")), "manual_since": doc.get("manual_since"),
        "policy": policy.get("action"), "expiry_proof_blockers": blockers,
        "expiry_proof_checked_at": doc.get("expiry_proof_checked_at"),
        "age_h": None if not (created and as_of) else round((as_of - created).total_seconds() / 3600.0, 2),
        "unresolved": doc.get("status") not in TERMINAL_INTENT or bool(doc.get("needs_manual_check")),
        "decision_holdings": doc.get("decision_holdings"),
    }


def _timeline(transitions):
    steps = []
    for key in sorted(transitions or {}):
        entry = transitions[key]
        if not isinstance(entry, dict):
            continue
        state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
        steps.append({"at": entry.get("at"), "rev": entry.get("revision"), "status": state.get("status"),
                      "broker_status": state.get("broker_status"), "cancel_error": state.get("cancel_last_error_code"),
                      "manual": state.get("needs_manual_check")})
    return steps


def analyze_orders(db, chain, as_of, run=None) -> dict:
    outbox = ((db.get("webull_lego_order_outbox") or {}).get(chain)) or {}
    transitions = ((db.get("webull_lego_execution_transitions") or {}).get(chain)) or {}
    views = [_intent_view(d, as_of) for d in outbox.values() if isinstance(d, dict)]
    views.sort(key=lambda v: v["created_at"] or "")
    if run:
        views = [v for v in views if v["run"].startswith(run[:8])]
    unresolved = [v for v in views if v["unresolved"]]
    for v in unresolved:
        full = next((k for k in outbox if short(k) == v["run"]), None)
        v["timeline"] = _timeline(transitions.get(full)) if full else []
    halts = []
    for scope, lock in (db.get("webull_lego_order_dispatch_locks") or {}).items():
        halt = lock.get("operator_halt") if isinstance(lock, dict) else None
        if isinstance(halt, dict) and halt.get("halted"):
            halts.append({"scope": short(scope, 14), "halt": short(halt.get("halt_id")), "reason": halt.get("reason"),
                          "set_at": halt.get("set_at"), "set_by": halt.get("set_by"),
                          "inflight": short(lock.get("inflight_run_id")), "fenced": short(lock.get("fenced_run_id"))})
    audit = []
    for scope, events in (db.get("webull_lego_operator_halt_audit") or {}).items():
        for event in (events or {}).values():
            if isinstance(event, dict):
                audit.append({"at": event.get("at"), "action": event.get("action"), "operator": event.get("operator"),
                              "reason": event.get("reason"), "halt": short(event.get("halt_id"))})
    audit.sort(key=lambda e: e["at"] or "")
    return {"n": len(views), "by_status": counted(v["status"] for v in views), "unresolved": unresolved,
            "halts": halts, "halt_audit": audit}


# ------------------------------------------------------------------------ RTDB: the flight recorder

def analyze_traces(db, *, run=None, since=None, limit=60) -> dict:
    docs = []
    for chain, days in (db.get("webull_lego_trace") or {}).items():
        for day, items in (days or {}).items():
            for key, doc in (items or {}).items():
                if isinstance(doc, dict):
                    docs.append({**doc, "_key": f"{short(chain, 14)}/{day}/{key}"})
    docs.sort(key=lambda d: d.get("at") or "")
    if since:
        docs = [d for d in docs if (parse_ts(d.get("at")) or since) >= since]
    if run:
        docs = [d for d in docs if any(str(r).startswith(run[:8]) for r in d.get("runs") or [])
                or any(str(e.get("run") or (e.get("x") or {}).get("run_id") or "").startswith(run[:8])
                       for e in d.get("events") or [] if isinstance(e, dict))]
    result = {"n": len(docs), "ticks": [], "exchanges": [], "proof": [], "errors": [], "warnings": {},
              "transitions": [], "equations": {"checked": 0, "mismatch": _bucket()}, "heartbeat": {}}
    exchanges, errors, warnings = {}, Counter(), Counter()
    for doc in docs:
        base = parse_ts(doc.get("at"))
        events = [e for e in doc.get("events") or [] if isinstance(e, dict)]
        result["ticks"].append({"at": doc.get("at"), "tick": short(doc.get("tick")), "http": doc.get("http"),
                                "pipe": doc.get("pipe"), "biz": doc.get("biz"), "why": doc.get("why"),
                                "path": doc.get("path"), "events": doc.get("n"), "dropped": doc.get("dropped"),
                                "partial": bool(doc.get("partial")), "rev": doc.get("rev")})
        for e in events:
            moment = None if base is None else iso(base + timedelta(milliseconds=num(e.get("t")) or 0))
            kind = e.get("k")
            if kind == "wb":
                err = e.get("err") if isinstance(e.get("err"), dict) else {}
                key = (e.get("op"), e.get("st"), err.get("code"))
                slot = exchanges.setdefault(key, {"n": 0, "first": moment, "last": moment, "ms": [], "request_ids": [],
                                                  "mutation": bool(e.get("mut"))})
                slot["n"] += 1
                slot["last"] = moment
                slot["ms"].append(num(e.get("ms")))
                if e.get("rid") and (err or e.get("mut")) and e["rid"] not in slot["request_ids"] \
                        and len(slot["request_ids"]) < 3:
                    slot["request_ids"].append(e["rid"])
            elif kind == "er":
                errors[(e.get("where") or e.get("n"), e.get("type"))] += 1
            elif kind == "wn":
                warnings[e.get("kind")] += 1
            elif kind == "tr":
                result["transitions"].append({"at": moment, "run": short(e.get("run")), "from": e.get("from"),
                                              "to": e.get("to"), "changed": e.get("chg")})
            elif kind == "n" and e.get("n") == "W11":
                facts = e.get("x") if isinstance(e.get("x"), dict) else {}
                result["proof"].append({"at": moment, "run": short(facts.get("run_id")), "tag": e.get("tag"),
                                        "blockers": facts.get("blockers") or ([e["tag"]] if e.get("ok") is False else []),
                                        "listed": facts.get("listed"), "open_orders": facts.get("open_orders"),
                                        "holdings": facts.get("holdings"), "decision_holdings": facts.get("decision_holdings"),
                                        "reason": facts.get("reason")})
            elif kind == "eq":
                _check_equation_event(e, result["equations"])
    result["exchanges"] = [{"op": k[0], "status": k[1], "error": k[2], "n": v["n"], "first": v["first"], "last": v["last"],
                            "ms": spread(v["ms"]), "request_ids": v["request_ids"], "mutation": v["mutation"]}
                           for k, v in sorted(exchanges.items(), key=lambda kv: (not kv[1]["mutation"], -kv[1]["n"]))]
    result["errors"] = [{"where": k[0], "type": k[1], "n": n} for k, n in errors.most_common(10)]
    result["warnings"] = dict(warnings)
    result["heartbeat"] = {short(c, 14): h for c, h in (db.get("webull_lego_heartbeat") or {}).items() if isinstance(h, dict)}
    result["ticks"] = result["ticks"][-limit:]
    return result


def _check_equation_event(event, report):
    given, out = event.get("in") or {}, event.get("out") or {}
    node = event.get("n")
    if node == "D08":
        try:
            expect = recompute_decision({**given, "P0": None if given.get("genesis") else given.get("P0"),
                                         "signal": given.get("signal")})
        except (KeyError, TypeError, ValueError):
            _mismatch(report["mismatch"], 20, node=node, field="inputs", recorded=None, expected="complete inputs")
            return
        report["checked"] += 1
        for field in ("V_n", "gap", "R_n", "qty"):
            if not close(expect[field], num(out.get(field)), 1e-9, 1e-6):
                _mismatch(report["mismatch"], 20, node=node, field=field, recorded=out.get(field), expected=expect[field])
        for field in ("status", "side"):
            if expect[field] != (out.get(field) or ""):
                _mismatch(report["mismatch"], 20, node=node, field=field, recorded=out.get(field), expected=expect[field])
        if given.get("A_prev") is not None and not close(num(out.get("A_n")), num(given["A_prev"]), 1e-9, 1e-7):
            _mismatch(report["mismatch"], 20, node=node, field="A_n_carried", recorded=out.get("A_n"), expected=given["A_prev"])
    elif node == "W13" and out.get("delta_actual") is not None:
        keys = ("FIX_C", "P_fill", "P_acted", "A_prev")
        values = [num(given.get(k)) for k in keys]
        reference = num(out.get("reference"))
        if None in values or reference is None or not values[2]:
            return
        report["checked"] += 1
        expect = recompute_fill(values[0], values[1], values[2], values[3], reference)
        for field in ("delta_actual", "actual_cumulative", "excess"):
            if not close(expect[field], num(out.get(field)), 1e-9, 1e-7):
                _mismatch(report["mismatch"], 20, node=node, field=field, recorded=out.get(field), expected=expect[field])


# -------------------------------------------------------------------------- deployment evidence

_SECRET_NAME = re.compile(r"(?i)(secret|token|password|credential|account|app_?key|authorization)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_service_yaml(text: str) -> dict:
    """The few facts a Cloud Run / Knative export gives (no YAML dependency, tolerant)."""
    lines = text.splitlines()
    env, secrets = {}, []
    start = next((i for i, line in enumerate(lines) if line.strip() == "env:"), None)
    if start is not None:
        base, item, i = _indent(lines[start]), None, start + 1

        def flush():
            if item is None:
                return
            if item["secret"]:
                secrets.append(item["name"])
            elif item["value"] is not None:
                env[item["name"]] = item["value"]

        while i < len(lines):
            line = lines[i]
            body = line.strip()
            if not body:
                i += 1
                continue
            indent = _indent(line)
            if indent < base or (indent == base and not body.startswith("- ")):
                break
            if body.startswith("- name:"):
                flush()
                item = {"name": body.split(":", 1)[1].strip(), "value": None, "secret": False}
            elif item is not None and body.startswith("value:"):
                rest = body[len("value:"):].strip()
                if rest in (">-", ">", "|", "|-", ">+", "|+"):          # folded/literal block scalar
                    parts, j = [], i + 1
                    while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > indent):
                        if lines[j].strip():
                            parts.append(lines[j].strip())
                        j += 1
                    item["value"] = " ".join(parts)
                    i = j
                    continue
                item["value"] = rest.strip("'\"")
            elif item is not None and body.startswith(("valueFrom:", "secretKeyRef:")):
                item["secret"] = True
            i += 1
        flush()

    def first(pattern):
        found = re.search(pattern, text, re.M)
        return found.group(1).strip("'\"") if found else None

    return {
        "kind": first(r"^kind:\s*(\S+)"), "revision": first(r"^    metadata:\n\s+name:\s*(\S+)"),
        "generation": first(r"^  generation:\s*(\d+)"),
        "timeout_s": num(first(r"timeoutSeconds:\s*(\d+)\s*$")), "concurrency": num(first(r"containerConcurrency:\s*(\d+)")),
        "cpu": first(r"^\s+cpu:\s*'?([\d.]+)"), "memory": first(r"^\s+memory:\s*(\S+)"),
        "max_scale": num(first(r"autoscaling\.knative\.dev/maxScale:\s*'?(\d+)")),
        "min_scale": num(first(r"autoscaling\.knative\.dev/minScale:\s*'?(\d+)")),
        "env": env, "secret_env": sorted(set(secrets)),
    }


def repo_candidate_hashes(repo: Path) -> dict:
    """tools.candidate_manifest's hash of ``repo``, with and without the sibling reader repo.

    The manifest also hashes ../lego-firebase-streamlit when it exists, so the same commit has two
    hashes. 2026-10-09 the deployed hash only matched the backend-only variant: it was built on a
    machine without the reader checkout.
    """
    try:
        sys.path.insert(0, str(repo))
        from tools import candidate_manifest as manifest
    except Exception:
        return {}
    finally:
        if sys.path and sys.path[0] == str(repo):
            sys.path.pop(0)
    saved = (manifest.ROOT, manifest.STREAMLIT)
    root = repo.resolve()
    reader = root.parent / "lego-firebase-streamlit"
    found = {}
    try:
        for name, sibling in (("backend_only", root.parent / "no-reader-checkout-here"),
                              ("with_reader", reader)):
            if name == "with_reader" and not reader.is_dir():
                continue
            manifest.ROOT, manifest.STREAMLIT = root, sibling
            try:
                found[name] = manifest.build_manifest()["candidate_hash"]
            except Exception:
                found[name] = None
    finally:
        manifest.ROOT, manifest.STREAMLIT = saved
    return found


def analyze_deploy(service, logs, repo, as_of) -> dict:
    env = service.get("env") or {}
    shown = {k: ("<set>" if _SECRET_NAME.search(k) else v) for k, v in sorted(env.items())
             if k.startswith(("LEGO_", "WEBULL_ENV", "ALERT_", "FIREBASE_")) and k != "FIREBASE_DB_URL"}
    window = parse_ts(env.get("LEGO_TRADING_WINDOW_END"))
    result = {"service": {k: service.get(k) for k in ("revision", "generation", "timeout_s", "concurrency", "cpu",
                                                      "memory", "max_scale", "min_scale")},
              "env": shown, "secret_env": service.get("secret_env"),
              "alert_webhook_configured": any(k.startswith("ALERT_WEBHOOK") for k in env),
              "git_commit": env.get("LEGO_GIT_COMMIT"), "candidate_hash": env.get("LEGO_CANDIDATE_HASH")}
    if window is not None and as_of is not None:
        result["window_end"] = iso(window)
        result["window_days_left"] = round((window - as_of).total_seconds() / 86400.0, 2)
    logged = {h for h in (logs or {}).get("revisions", {})}
    if service.get("revision") and logged:
        result["revision_in_logs"] = service["revision"] in logged
    if repo is not None:
        head, dirty = git_state(Path(repo))
        hashes = repo_candidate_hashes(Path(repo))
        result["repo_candidate_hashes"] = hashes
        result["repo_commit"], result["repo_dirty"] = head, dirty
        deployed = env.get("LEGO_GIT_COMMIT")
        if hashes and env.get("LEGO_CANDIDATE_HASH"):
            comparable = bool(head and deployed and head == deployed and not dirty)
            result["candidate_comparable"] = comparable
            if comparable:
                matched = [name for name, h in hashes.items() if h and h == env["LEGO_CANDIDATE_HASH"]]
                result["candidate_matches_repo"] = bool(matched)
                result["candidate_variant"] = matched[0] if matched else None
    return result


def git_state(repo: Path):
    """(HEAD commit, working tree has changes) of ``repo``; (None, None) when git cannot say."""
    import subprocess
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, timeout=20)
        if head.returncode or dirty.returncode:
            return None, None
        return head.stdout.strip(), bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None


# ----------------------------------------------------------------------------- the whole report

def analyze_rtdb(db, *, diff=None, fix_c=None, as_of=None, run=None, since=None) -> dict:
    result = {"collections": {k: (len(v) if isinstance(v, dict) else 1) for k, v in sorted(db.items())},
              "chains": []}
    for chain, state in sorted((db.get("webull_lego_state") or {}).items()):
        if not isinstance(state, dict):
            continue
        rows = [r for r in (db.get("webull_lego_rows") or {}).values()
                if isinstance(r, dict) and r.get("chain_key") == chain]
        outbox = ((db.get("webull_lego_order_outbox") or {}).get(chain)) or {}
        intents = {k: v for k, v in outbox.items() if isinstance(v, dict)}
        realized = (db.get("webull_lego_realized") or {}).get(chain) or {}
        p0 = num(state.get("p0"))
        rows_report = check_rows(rows, fix_c=fix_c, diff=diff, p0=p0)
        fixed = rows_report.get("fix_c")
        fills_report = check_fills(state, rows, intents, realized, fix_c=fixed, p0=p0)
        result["chains"].append({
            "chain": short(chain, 14),
            "state": {"updated_at": state.get("updated_at"), "version": state.get("version"), "dna_step": state.get("dna_step"),
                      "slot_id": state.get("slot_id"), "semantics": state.get("cashflow_semantics"),
                      "clock_mode": state.get("clock_mode"), "holdings": state.get("prev_holdings"), "p0": p0},
            "rows": rows_report, "fills": fills_report, "ledger": check_ledger(state, realized, fills_report),
            "economics": economics(realized), "orders": analyze_orders(db, chain, as_of, run),
        })
    result["traces"] = analyze_traces(db, run=run, since=since)
    return result


def _rtdb_latest(db):
    """Newest timestamp anywhere in the export's operational documents (the export time is later than the logs)."""
    stamps = []
    for chain in (db.get("webull_lego_state") or {}).values():
        stamps.append(parse_ts((chain or {}).get("updated_at")))
    for chain in (db.get("webull_lego_order_outbox") or {}).values():
        for doc in (chain or {}).values():
            if isinstance(doc, dict):
                stamps += [parse_ts(doc.get("updated_at")), parse_ts(doc.get("manual_since"))]
    for lock in (db.get("webull_lego_order_dispatch_locks") or {}).values():
        if isinstance(lock, dict):
            halt = lock.get("operator_halt") if isinstance(lock.get("operator_halt"), dict) else {}
            stamps += [parse_ts(lock.get("claimed_at")), parse_ts(lock.get("fenced_at")), parse_ts(halt.get("set_at"))]
    for days in (db.get("webull_lego_trace") or {}).values():
        for items in (days or {}).values():
            stamps += [parse_ts(doc.get("at")) for doc in (items or {}).values() if isinstance(doc, dict)]
    for beat in (db.get("webull_lego_heartbeat") or {}).values():
        stamps.append(parse_ts((beat or {}).get("at")))
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def _latest_moment(logs_report, db):
    """'Now' for an offline report: the newest timestamp any input carries."""
    stamps = [parse_ts(((logs_report or {}).get("ticks") or {}).get("last")), _rtdb_latest(db or {})]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def build_findings(report) -> list[dict]:
    out = []

    def add(level, code, text, evidence=None):
        out.append({"level": level, "code": code, "text": text, **({"evidence": evidence} if evidence else {})})

    rtdb, logs, deploy = report.get("rtdb") or {}, report.get("logs") or {}, report.get("deploy") or {}
    traces = rtdb.get("traces") or {}
    proof_by_run = {}
    for p in traces.get("proof") or []:
        proof_by_run[p["run"]] = p
    for chain in rtdb.get("chains", []):
        for o in chain["orders"]["unresolved"]:
            held = f"{o['status']}, broker {o['broker_status']}, age {o['age_h']} h" if o["age_h"] is not None else o["status"]
            stuck = o["needs_manual"] or o["age_h"] is None or o["age_h"] * 60 > IN_FLIGHT_MINUTES
            add("P0" if stuck else "P2", "ORDER_UNRESOLVED" if stuck else "ORDER_IN_FLIGHT",
                f"order {o['run']} ({o['side']} {o['qty']}) is {'unresolved' if stuck else 'still in flight'}: {held}; "
                f"cancel attempts {o['cancel_attempts']}, last error {o['cancel_error']}"
                + (f", manual since {o['manual_since']}" if o["needs_manual"] else ""),
                {"chain": chain["chain"], "broker_id": o["broker_id"]})
            blockers = o["expiry_proof_blockers"] or (proof_by_run.get(o["run"]) or {}).get("blockers")
            if blockers:
                add("P0", "EXPIRY_PROOF_BLOCKED", f"order {o['run']}: the DAY-expiry proof is not met because: "
                    f"{', '.join(map(str, blockers))}", {"checked_at": o["expiry_proof_checked_at"]})
            elif stuck and (o["cancel_refused_at"] or o["needs_manual"]) and (
                    o["policy"] == "cancel_expire" or (deploy.get("env") or {}).get("LEGO_STALE_ORDER_ACTION") == "cancel_expire"):
                add("P0", "EXPIRY_PROOF_UNKNOWN", f"order {o['run']}: cancel_expire is configured but nothing recorded WHICH "
                    "proof condition fails (no intent.expiry_proof_blockers, no W11 trace). Deploy a build with the flight "
                    "recorder and run tools.resume_order_reconciliation --expiry-proof (read-only dry run)")
        for h in chain["orders"]["halts"]:
            blocked = (chain["rows"].get("gated") or {}).get("PASS_RECOVERY_BLOCKED", 0)
            add("P0", "OPERATOR_HALT", f"operator halt {h['halt']} ({h['reason']}) set {h['set_at']} by {h['set_by']}; "
                f"{blocked} slots were decided but blocked", {"inflight": h["inflight"]})
        if chain["rows"]["mismatch"]["count"]:
            first = chain["rows"]["mismatch"]["items"][0]
            add("P0", "ROW_EQUATION_MISMATCH", f"{chain['rows']['mismatch']['count']} row equation mismatches; first: {first}")
        if chain["fills"]["mismatch"]["count"]:
            first = chain["fills"]["mismatch"]["items"][0]
            add("P0", "FILL_EQUATION_MISMATCH", f"{chain['fills']['mismatch']['count']} fill/ledger mismatches; first: {first}")
        if chain["ledger"]["problems"]:
            add("P0", "LEDGER_INCONSISTENT", "; ".join(chain["ledger"]["problems"]))
        rows_report = chain["rows"]
        if rows_report.get("n") and not (rows_report.get("versions", {}).get("contiguous") and rows_report.get("steps", {}).get("increasing")):
            add("P1", "ROWS_NOT_CONTIGUOUS", f"row versions/steps are not contiguous: {rows_report.get('steps', {}).get('gaps')}")
        if rows_report.get("uncommitted"):
            add("P1", "ROWS_UNCOMMITTED", f"{rows_report['uncommitted']} rows are not committed")
        eco = chain["economics"]
        if eco.get("n") and eco["fee_pct"]["median"] >= 0.3:
            add("P1", "FEE_DRAG", f"median fee {eco['fee_pct']['median']}% per order, ~{eco['round_trip_fee_pct']}% per round trip; "
                f"realized after fees {eco['cumulative_realized']} over {eco['n']} fills (UAT fees are not PROD fees: gate G6 "
                "needs a measured PROD fee before real money)")
    ticks = logs.get("ticks") or {}
    errors = (ticks.get("by_severity") or {}).get("ERROR", 0)
    if deploy and not deploy.get("alert_webhook_configured"):
        add("P1", "ALERTING_NOT_CONFIGURED", f"no ALERT_WEBHOOK_* in the service env; {errors} ERROR ticks and any unresolved "
            "order paged nobody")
    elif not deploy and errors:
        add("INFO", "ALERTING_UNKNOWN", f"{errors} ERROR ticks; no service YAML given, so alert delivery is unverified")
    left = deploy.get("window_days_left")
    if left is not None and left <= 7:
        add("P1" if left > 0 else "P0", "RELEASE_WINDOW", f"the trading window ends {deploy['window_end']} ({left} days left)")
    if deploy.get("candidate_matches_repo") is False:
        add("P1", "CANDIDATE_MISMATCH", f"the checkout is the deployed commit but no candidate hash it produces "
            f"({', '.join(f'{k} {str(v)[:10]}' for k, v in deploy['repo_candidate_hashes'].items())}) equals the one the service runs: "
            "the deployed bits are not this source")
    elif deploy.get("candidate_comparable") is False:
        why = "has uncommitted changes" if deploy.get("repo_commit") == deploy.get("git_commit") else "is another commit"
        add("INFO", "CANDIDATE_NOT_COMPARED", f"the checkout ({str(deploy.get('repo_commit'))[:10]}) {why}; the service runs "
            f"{str(deploy.get('git_commit'))[:10]}. Check out the deployed commit, clean, to verify the candidate hash")
    if ticks.get("gap_count"):
        add("P2", "TICK_GAPS", f"{ticks['gap_count']} gaps longer than 3× the {ticks['interval_s']} s tick interval; worst: {ticks['gaps'][:2]}")
    deferred = (ticks.get("by_business") or {}).get("TICK_DEFERRED", 0)
    if deferred:
        add("P2", "TICKS_DEFERRED", f"{deferred} ticks ran out of time budget and deferred their work to the next tick")
    if ticks.get("near_budget"):
        add("P2", "TICK_NEAR_BUDGET", f"{ticks['near_budget']} ticks used more than 70% of the {TICK_BUDGET_SECONDS:.0f} s budget; slowest: {ticks['slowest'][:2]}")
    for e in (logs.get("webull") or {}).get("errors", [])[:5]:
        add("P2", "WEBULL_ERRORS", f"{e['n']}× broker error op={e['op']} code={e['code']} http={e['http']} "
            f"({e['first']} … {e['last']}) request ids {e['request_ids']}")
    for e in (logs.get("webull") or {}).get("sdk_errors", [])[:5]:
        add("P2", "WEBULL_SDK_ERRORS", f"{e['n']}× SDK error route={e['route']} code={e['code']} http={e['http']} "
            f"({e['first']} … {e['last']}) request ids {e['request_ids']}")
    equations = traces.get("equations") or {}
    if equations.get("mismatch", {}).get("count"):
        add("P0", "TRACE_EQUATION_MISMATCH", f"{equations['mismatch']['count']} recorded equations do not recompute; "
            f"first: {equations['mismatch']['items'][0]}")
    for chain, beat in (traces.get("heartbeat") or {}).items():
        bad = {k: beat.get(k) for k in ("write_errors", "flush_timeouts", "internal_errors") if beat.get(k)}
        if bad:
            add("P2", "RECORDER_UNHEALTHY", f"flight recorder counters on {chain}: {bad}")
    if not traces.get("n") and (rtdb.get("chains") or deploy):
        add("INFO", "NO_TRACES", "no flight-recorder traces in this export: the reason an order stays unresolved cannot be read "
            "from Firebase until a build with the recorder is deployed")
    if (deploy.get("env") or {}).get("WEBULL_ENV") == "UAT":
        add("INFO", "UAT_ONLY", "this is UAT evidence; real money also needs real alert delivery, observed PROD sessions and a "
            "measured PROD fee (gates G4-G6) which no UAT export can supply")
    order = {level: i for i, level in enumerate(LEVELS)}
    out.sort(key=lambda f: order[f["level"]])
    return out


def build_report(*, logs=None, rtdb=None, service_text=None, repo=None, inputs=None, since=None,
                 run=None, diff=None, fix_c=None) -> dict:
    report = {"inputs": inputs or []}
    if logs is not None:
        report["logs"] = analyze_logs(logs, since=since)
    service = parse_service_yaml(service_text) if service_text else None
    if service:
        diff = diff if diff is not None else num(service["env"].get("LEGO_DIFF"))
        fix_c = fix_c if fix_c is not None else num(service["env"].get("LEGO_FIX_C"))
    as_of = _latest_moment(report.get("logs"), rtdb)
    report["as_of"] = iso(as_of)
    if rtdb is not None:
        report["rtdb"] = analyze_rtdb(rtdb, diff=diff, fix_c=fix_c, as_of=as_of, run=run, since=since)
    if service:
        report["deploy"] = analyze_deploy(service, report.get("logs"), repo, as_of)
    elif repo is not None:
        report["deploy"] = {"repo_candidate_hashes": repo_candidate_hashes(Path(repo))}
    findings = build_findings(report)
    report["findings"] = findings
    report["summary"] = {level: sum(1 for f in findings if f["level"] == level) for level in LEVELS}
    return report


# ------------------------------------------------------------------------------------ rendering

def _kv(mapping, limit=None):
    items = list(mapping.items())[:limit]
    return ", ".join(f"{k}={v}" for k, v in items) if items else "-"


def render(report: dict) -> str:
    out = []
    s = report["summary"]
    out.append(f"AUDIT DIGEST  as of {report.get('as_of')}   {s['P0']} P0 · {s['P1']} P1 · {s['P2']} P2 · {s['INFO']} info")
    for f in report["findings"]:
        out.append(f"  [{f['level']}] {f['code']}: {f['text']}")
    out.append("")
    out.append("== inputs")
    for item in report["inputs"]:
        out.append(f"  {item['name']}: {item['bytes']} bytes sha256 {item['sha256'][:16]}… {item.get('note', '')}")
    logs = report.get("logs")
    if logs:
        t, h = logs["ticks"], logs["http"]
        out.append("")
        out.append(f"== ticks (Cloud Logging, {logs['entries']} entries)")
        if t.get("n"):
            out.append(f"  {t['n']} ticks {t['first']} → {t['last']} every ~{t['interval_s']} s; instances {len(logs['instances'])}; "
                       f"revisions {_kv(logs['revisions'])}")
            out.append(f"  severity {_kv(t['by_severity'])} | http {_kv(t['by_http'])} | business {_kv(t['by_business'], 8)}")
            out.append(f"  duration ms p50/p95/max {t['duration_ms']['p50']}/{t['duration_ms']['p95']}/{t['duration_ms']['max']}; "
                       f"mode {_kv(t['mode'])} env {_kv(t['environment'])}; halt_since {t['halt_since']}")
            out.append("  status segments: " + " → ".join(f"{x['status']}×{x['n']}" for x in t["segments"][:14])
                       + (" …" if len(t["segments"]) > 14 else ""))
            for r in t["error_runs"][:6]:
                out.append(f"  error run {r['from']} → {r['to']} ×{r['n']} pipeline {_kv(r['pipeline'])} types {_kv(r['types'])}")
        if h.get("n"):
            out.append(f"  http requests {h['n']}: {_kv(h.get('by_status', {}))}; latency s {h.get('latency_s')}")
        out.append("")
        out.append("== operations and Webull errors (Cloud Logging)")
        for name, row in list(logs["webull"]["operations"].items())[:14]:
            extra = f" codes {row['codes']} http {row['http']} rid {row['request_ids']}" if row["errors"] else ""
            out.append(f"  {name:<22} n={row['n']:<5} err={row['errors']:<4} ms p50/p95/max "
                       f"{(row['ms'] or {}).get('p50')}/{(row['ms'] or {}).get('p95')}/{(row['ms'] or {}).get('max')}{extra}")
        for e in logs["webull"]["errors"][:6]:
            out.append(f"  tick-level broker error op={e['op']} code={e['code']} http={e['http']} ×{e['n']} {e['first']}…{e['last']} rid {e['request_ids']}")
        for e in logs["webull"]["sdk_errors"][:8]:
            out.append(f"  sdk text log error route={e['route']} code={e['code']} http={e['http']} ×{e['n']} {e['first']}…{e['last']} rid {e['request_ids']}")
    rtdb = report.get("rtdb")
    if rtdb:
        for chain in rtdb["chains"]:
            r, f, led, eco, orders = chain["rows"], chain["fills"], chain["ledger"], chain["economics"], chain["orders"]
            out.append("")
            out.append(f"== chain {chain['chain']}  v{chain['state']['version']} step {chain['state']['dna_step']} "
                       f"slot {chain['state']['slot_id']} {chain['state']['semantics']}")
            out.append(f"  equations: {r['n']} rows, FIX_C={r['fix_c']}, DIFF={r['diff'].get('used')} ({r['diff'].get('source')}, "
                       f"rows allow [{r['diff'].get('lower')}, {r['diff'].get('upper')})), P0={r['p0']}, qty rules {r['rules']}, "
                       f"gated {r['gated']}: {'OK' if not r['mismatch']['count'] else str(r['mismatch']['count']) + ' MISMATCH'}")
            for m in r["mismatch"]["items"][:5]:
                out.append(f"    ! {m}")
            if r.get("versions"):
                out.append(f"  rows: versions {r['versions']['first']}…{r['versions']['last']} contiguous={r['versions']['contiguous']}; "
                           f"steps {r['steps']['first']}…{r['steps']['last']} gaps={r['steps']['gaps']}; uncommitted {r['uncommitted']}; "
                           f"cashflow {_kv(r['cashflow_status'])}; status {_kv(r['status_counts'])}")
            out.append(f"  fills: {f['n']} finalized (seq {f['finalized_seq']}/applied {f['applied_seq']}): "
                       f"{'OK' if not f['mismatch']['count'] else str(f['mismatch']['count']) + ' MISMATCH'}")
            for m in f["mismatch"]["items"][:5]:
                out.append(f"    ! {m}")
            out.append(f"  ledger: applied {led['applied']}, net position change {led['net_position_change']}, realized "
                       f"{led['cumulative_realized']}: {'OK' if not led['problems'] else '; '.join(led['problems'])}")
            if eco.get("n"):
                out.append(f"  economics: {eco['n']} fills, notional {eco['notional_total']}, fees {eco['fees_total']}, fee/order % "
                           f"{eco['fee_pct']}, round trip ≈ {eco['round_trip_fee_pct']}%, realized (after fees) {eco['cumulative_realized']}")
            out.append(f"  orders: {orders['n']} intents {_kv(orders['by_status'])}")
            for o in orders["unresolved"]:
                out.append(f"  UNRESOLVED {o['run']} {o['side']} {o['qty']} {o['status']} broker={o['broker_status']} id…{o['broker_id']} "
                           f"created {o['created_at']} placed {o['placed_at']} age {o['age_h']} h")
                out.append(f"    cancel attempts {o['cancel_attempts']} refused {o['cancel_refused_at']} error {o['cancel_error']} "
                           f"hold until {o['hold_deadline']} manual since {o['manual_since']}")
                out.append(f"    expiry proof blockers {o['expiry_proof_blockers']} checked {o['expiry_proof_checked_at']}")
                for step in o.get("timeline", [])[:12]:
                    out.append(f"    {step['at']} r{step['rev']} {step['status']} broker={step['broker_status']} {step['cancel_error'] or ''}")
            for h in orders["halts"]:
                out.append(f"  HALT {h['halt']} {h['reason']} set {h['set_at']} by {h['set_by']} (inflight {h['inflight']}, fenced {h['fenced']})")
            for e in orders["halt_audit"][-4:]:
                out.append(f"    audit {e['at']} {e['action']} {e['operator']} {e['reason']}")
        tr = rtdb["traces"]
        out.append("")
        out.append(f"== flight recorder ({tr['n']} traces)")
        for chain, beat in tr["heartbeat"].items():
            out.append(f"  heartbeat {chain}: last {beat.get('at')} ticks {beat.get('ticks')} written {beat.get('written')} "
                       f"dup {beat.get('skipped_dup')} budget {beat.get('skipped_budget')} write_err {beat.get('write_errors')} "
                       f"timeouts {beat.get('flush_timeouts')} internal {beat.get('internal_errors')} rev {beat.get('rev')}")
        for t in tr["ticks"][-12:]:
            out.append(f"  {t['at']} {t['tick']} http={t['http']} {t['pipe']}/{t['biz']} why={t['why']} events={t['events']}"
                       f"{' PARTIAL' if t['partial'] else ''}")
            out.append(f"      {t['path']}")
        for p in tr["proof"][-8:]:
            out.append(f"  W11 {p['at']} run {p['run']} blockers {p['blockers']} listed={p['listed']} open_orders={p['open_orders']} "
                       f"holdings={p['holdings']} decision_holdings={p['decision_holdings']} {p['reason'] or ''}")
        for e in tr["exchanges"][:14]:
            out.append(f"  webull {e['op']:<14} http={e['status']} err={e['error']} ×{e['n']} ms {e['ms']} rid {e['request_ids']}"
                       f"{' MUTATION' if e['mutation'] else ''}")
        for x in tr["transitions"][-10:]:
            out.append(f"  S01 {x['at']} {x['run']} {x['from']} → {x['to']} {x['changed'] or ''}")
        if tr["errors"]:
            out.append("  caught errors: " + ", ".join(f"{e['where']}:{e['type']}×{e['n']}" for e in tr["errors"]))
        if tr["warnings"]:
            out.append("  warnings: " + _kv(tr["warnings"]))
        eq = tr["equations"]
        out.append(f"  recorded equations recomputed: {eq['checked']} → {'OK' if not eq['mismatch']['count'] else str(eq['mismatch']['count']) + ' MISMATCH'}")
    deploy = report.get("deploy")
    if deploy:
        out.append("")
        out.append("== deployment")
        out.append(f"  service {deploy.get('service')}")
        out.append(f"  env {deploy.get('env')}  secrets {deploy.get('secret_env')}")
        out.append(f"  candidate {str(deploy.get('candidate_hash'))[:16]}… git {str(deploy.get('git_commit'))[:12]} "
                   f"repo {deploy.get('repo_candidate_hashes')} match={deploy.get('candidate_matches_repo')} "
                   f"({deploy.get('candidate_variant')}); "
                   f"window end {deploy.get('window_end')} ({deploy.get('window_days_left')} days); alert webhook {deploy.get('alert_webhook_configured')}")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------- live

def read_live(reader, *, chain=None, last=10) -> dict:
    """Heartbeat and the newest traces straight from the database. ``reader(path)`` -> JSON."""
    heartbeat = reader("webull_lego_heartbeat") or {}
    chains = [chain] if chain else sorted(heartbeat)
    db = {"webull_lego_heartbeat": heartbeat, "webull_lego_trace": {}}
    for name in chains:
        days = reader(f"webull_lego_trace_days/{name}") or {}
        if not days:
            continue
        day = max(days)
        items = reader(f"webull_lego_trace/{name}/{day}") or {}
        newest = dict(sorted(items.items())[-last:])
        db["webull_lego_trace"][name] = {day: newest}
    return analyze_traces(db, limit=last)


def render_live(live: dict) -> str:
    out = []
    for chain, beat in live["heartbeat"].items():
        out.append(f"heartbeat {chain}: last {beat.get('at')} {beat.get('pipe')}/{beat.get('biz')} ticks {beat.get('ticks')} "
                   f"written {beat.get('written')} errors {beat.get('write_errors')}/{beat.get('internal_errors')} rev {beat.get('rev')}")
    for t in live["ticks"]:
        out.append(f"{t['at']} {t['tick']} http={t['http']} {t['pipe']}/{t['biz']} why={t['why']}")
        out.append(f"    {t['path']}")
    for p in live["proof"][-3:]:
        out.append(f"W11 {p['at']} run {p['run']} blockers {p['blockers']}")
    return "\n".join(out) if out else "no traces yet"


def firebase_reader():
    import os
    import firebase_admin
    from firebase_admin import db
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"databaseURL": os.environ["FIREBASE_DB_URL"]})
    return lambda path: db.reference(path).get()


def follow(reader, *, chain=None, interval=15.0, last=10, sleep=time.sleep, out=print, rounds=None):
    seen, count = set(), 0
    while rounds is None or count < rounds:
        live = read_live(reader, chain=chain, last=last)
        fresh = [t for t in live["ticks"] if (t["at"], t["tick"]) not in seen]
        seen.update((t["at"], t["tick"]) for t in live["ticks"])
        if fresh:
            out(render_live({**live, "ticks": fresh}))
        count += 1
        if rounds is None or count < rounds:
            sleep(interval)


# ------------------------------------------------------------------------------------------ CLI

def load_input(path: str, kind: str):
    raw = Path(path).read_bytes()
    item = {"name": f"{kind}:{Path(path).name}", "bytes": len(raw), "sha256": sha256_bytes(raw)}
    if kind == "yaml":
        return raw.decode("utf-8", "replace"), item
    return json.loads(raw), item


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only audit digest of a LEGO run.")
    parser.add_argument("--logs"), parser.add_argument("--rtdb"), parser.add_argument("--yaml")
    parser.add_argument("--repo", help="checkout to compare the deployed candidate hash with")
    parser.add_argument("--since", help="ISO time; ignore older logs and traces")
    parser.add_argument("--run", help="run id prefix: only that order's timeline and traces")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-on", choices=LEVELS[:3], help="exit 1 when a finding of this level or worse exists")
    parser.add_argument("--live", action="store_true", help="read the database now (needs Firebase credentials)")
    parser.add_argument("--follow", action="store_true", help="with --live: keep printing new traces")
    parser.add_argument("--chain", help="with --live: chain key")
    args = parser.parse_args(argv)
    if args.live:
        reader = firebase_reader()
        if args.follow:
            follow(reader, chain=args.chain)
            return 0
        live = read_live(reader, chain=args.chain)
        print(json.dumps(live, ensure_ascii=False, indent=1, default=str) if args.json else render_live(live))
        return 0
    if not (args.logs or args.rtdb or args.yaml or args.repo):
        parser.error("give at least one of --logs --rtdb --yaml --repo, or --live")
    inputs, logs, rtdb, service_text = [], None, None, None
    for kind, path in (("logs", args.logs), ("rtdb", args.rtdb), ("yaml", args.yaml)):
        if path:
            value, item = load_input(path, kind)
            inputs.append(item)
            if kind == "logs":
                logs = value
            elif kind == "rtdb":
                rtdb = value
            else:
                service_text = value
    report = build_report(logs=logs, rtdb=rtdb, service_text=service_text, repo=args.repo, inputs=inputs,
                          since=parse_ts(args.since), run=args.run)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str) if args.json else render(report))
    if args.fail_on:
        worst = LEVELS[:LEVELS.index(args.fail_on) + 1]
        return 1 if any(f["level"] in worst for f in report["findings"]) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
