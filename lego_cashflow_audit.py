"""Read-only audit of a committed chain against execution_confirmed_v1.

Nothing here writes. A contaminated row is evidence, and the repair for one is a
reviewed migration, not a background job that quietly rewrites history — so this
module answers one question per row per column ("is the stored number the one
execution_confirmed_v1 requires?") and stops there.

It exists because the corruption it looks for is invisible in the places an
operator actually looks. A row written by an older revision carries a plausible
ΔAₙ, `committed: true`, no error field, and an HTTP 200 in the logs; the only
sign is that the arithmetic does not close. Re-deriving that by hand from a CSV
export is how the incident this module was written for was found.

Two deliberate choices about what counts as ground truth:

* **Aₙ₋₁ comes from the previous row as stored**, not from a replay of the whole
  chain. Each row is then judged against the state its own writer actually saw,
  so one bad row is reported as one bad row instead of poisoning every row after
  it. Replaying from genesis answers a different question ("what should this
  chain look like?") and would bury the first divergence under its consequences.
* **P_acted is reconstructed**, because no row stores it. It advances only on a
  row carrying broker-confirmed fill evidence, which is exactly the rule under
  audit — so a chain that dragged P_acted forward on PASS rows shows up as a
  ΔAₙ mismatch on the row *after* the one that did it, which is where the money
  actually went wrong.

A row counts as executed only with fill evidence on the row itself:
`cashflow_status == FINALIZED`, or a positive `execution_quantity`. READY_BUY,
READY_SELL, SUBMITTED, PENDING_DISPATCH, a rejection and an expiry are all
intent, and intent moves nothing.
"""
from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass, field

from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN,
                          PASS_DNA_ZERO, PASS_THRESHOLD, REFERENCE_COLUMN)

PRICE_COLUMN = "ราคา Pₙ (USD)"
STATUS_COLUMN = "สถานะ"
STEP_COLUMN = "DNA step"

CASHFLOW_FINALIZED = "FINALIZED"

# Absolute slack on a money column, for float noise in a full-precision read.
MONEY_TOLERANCE = 1e-6
# Half a cent: what a price loses to the 2-dp rounding the dashboard and the CSV
# export apply. Pass it as `price_quantum` when auditing an export rather than
# RTDB, and every band below widens by however much that rounding could have
# moved the column. Rₙ on a $340 stock with fix_c = 3000 moves by about 4 cents
# from price rounding alone — enough to look like a violation, and nowhere near
# what a wrong branch produces (the incident behind this module shows −2.72
# where 0.00 belongs). The frozen ΔAₙ = 0 check keeps a band of exactly zero
# either way: it does not read a price, so no rounding can excuse it.
DISPLAY_PRICE_QUANTUM = 0.005


class AuditInputError(ValueError):
    """The rows handed in cannot be audited (missing columns, no P₀, ...)."""


@dataclass(frozen=True)
class RowAudit:
    index: int
    run_id: str
    version: int | None
    status: str
    price: float
    executed: bool
    expected: dict[str, float]
    stored: dict[str, float]
    mismatched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.mismatched


def _number(row: dict, key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditInputError(f"แถวไม่มีคอลัมน์ตัวเลข {key!r} ที่ใช้ได้") from exc


def _optional_number(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def row_is_executed(row: dict) -> bool:
    """Broker-confirmed fill evidence on the row itself, or nothing.

    Deliberately blind to `สถานะ`: treating READY_* as executed is the bug class
    this whole audit exists to detect, so reading it here would make the audit
    agree with whatever wrote the row.
    """
    if str(row.get("cashflow_status") or "") == CASHFLOW_FINALIZED:
        return True
    quantity = _optional_number(row, "execution_quantity")
    return quantity is not None and quantity > 0


def _executed_price(row: dict) -> float:
    """The price a fill was booked at — the filled one, never the decision's."""
    price = _optional_number(row, "execution_price")
    if price is None or not (math.isfinite(price) and price > 0):
        raise AuditInputError(
            f"แถว {row.get('run_id')} มีหลักฐาน fill แต่ไม่มี execution_price ที่ใช้ได้")
    return price


def audit_rows(fix_c: float, rows: list[dict], *, p0: float | None = None,
               last_action_price: float | None = None,
               actual_cumulative: float | None = None,
               tolerance: float = MONEY_TOLERANCE,
               price_quantum: float = 0.0) -> list[RowAudit]:
    """Judge every row of one chain, in version order, column by column.

    `rows` must be one chain's committed rows already sorted by version. `p0`,
    `last_action_price` and `actual_cumulative` seed a chain whose genesis row is
    not in the slice; with a genesis row present (DNA step 0) they are read from
    it and may be omitted.

    `price_quantum` is how much each price may already have been rounded — 0 for
    a full-precision RTDB read, DISPLAY_PRICE_QUANTUM for a 2-dp CSV export.
    """
    if not (math.isfinite(fix_c) and fix_c > 0):
        raise AuditInputError("fix_c ต้อง finite และ > 0")
    if not rows:
        return []

    first_step = rows[0].get(STEP_COLUMN)
    genesis = first_step not in (None, "") and int(float(first_step)) == 0
    if p0 is None:
        if not genesis:
            raise AuditInputError(
                "ไม่มีแถว genesis (DNA step 0) — ต้องระบุ p0 จาก webull_lego_state")
        p0 = _number(rows[0], PRICE_COLUMN)
    if not (math.isfinite(p0) and p0 > 0):
        raise AuditInputError("p0 ต้อง finite และ > 0")

    acted_price = p0 if last_action_price is None else float(last_action_price)
    cumulative = 0.0 if actual_cumulative is None else float(actual_cumulative)

    results: list[RowAudit] = []
    for i, row in enumerate(rows):
        price = _number(row, PRICE_COLUMN)
        if not (math.isfinite(price) and price > 0):
            raise AuditInputError(f"แถว {row.get('run_id')} มีราคาที่ใช้ไม่ได้")
        stored = {
            REFERENCE_COLUMN: _number(row, REFERENCE_COLUMN),
            DELTA_COLUMN: _number(row, DELTA_COLUMN),
            ACTUAL_COLUMN: _number(row, ACTUAL_COLUMN),
            EXCESS_COLUMN: _number(row, EXCESS_COLUMN),
        }
        executed = row_is_executed(row)

        # Widths, per column, of what price rounding alone could have moved —
        # d/dP of each formula times the quantum. Zero for a value no price
        # feeds into, which is why a frozen ΔAₙ is still checked against exact 0.
        slack = fix_c * price_quantum
        if i == 0 and genesis:
            # The chain's own zero point: no reference price behind it yet.
            expected = {REFERENCE_COLUMN: 0.0, DELTA_COLUMN: 0.0,
                        ACTUAL_COLUMN: 0.0, EXCESS_COLUMN: 0.0}
            band = dict.fromkeys(expected, 0.0)
            next_acted = price
        else:
            reference = fix_c * math.log(price / p0)
            reference_band = slack * (1.0 / price + 1.0 / p0)
            if executed:
                filled = _executed_price(row)
                delta = fix_c * (filled / acted_price - 1.0)
                actual = cumulative + delta
                # Against the row's *own committed* Rₙ, which is what
                # finalize_recurrence books: Rₙ is the engine's column and a
                # fill never recomputes it.
                excess = actual - stored[REFERENCE_COLUMN]
                delta_band = slack * (1.0 + filled / acted_price) / acted_price
                excess_band = delta_band
                next_acted = filled
            else:
                delta = 0.0
                actual = cumulative
                excess = cumulative - fix_c * math.log(acted_price / p0)
                delta_band = 0.0
                excess_band = slack * (1.0 / acted_price + 1.0 / p0)
                next_acted = acted_price
            expected = {REFERENCE_COLUMN: reference, DELTA_COLUMN: delta,
                        ACTUAL_COLUMN: actual, EXCESS_COLUMN: excess}
            band = {REFERENCE_COLUMN: reference_band, DELTA_COLUMN: delta_band,
                    ACTUAL_COLUMN: delta_band, EXCESS_COLUMN: excess_band}

        mismatched = tuple(
            column for column, want in expected.items()
            if abs(stored[column] - want) > tolerance + band[column])
        results.append(RowAudit(
            index=i,
            run_id=str(row.get("run_id") or ""),
            version=None if row.get("version") in (None, "") else int(float(row["version"])),
            status=str(row.get(STATUS_COLUMN) or ""),
            price=price,
            executed=executed,
            expected=expected,
            stored=stored,
            mismatched=mismatched,
        ))
        # Aₙ carries from what this row *stored*, so the next row is judged
        # against the state its writer saw and one bad row stays one bad row.
        # P_acted cannot: no row stores it, so it follows the rule under audit.
        acted_price = next_acted
        cumulative = stored[ACTUAL_COLUMN]

    return results


def frozen_row_violations(results: list[RowAudit]) -> list[RowAudit]:
    """Rows with no fill that moved ΔAₙ or Aₙ anyway — the incident signature."""
    return [r for r in results
            if not r.executed
            and (DELTA_COLUMN in r.mismatched or ACTUAL_COLUMN in r.mismatched)]


def format_report(results: list[RowAudit]) -> str:
    """Plain-text evidence, one line per row, mismatches spelled out."""
    lines = [
        f"{'#':>3} {'version':>7} {'สถานะ':<16} {'fill':<5} ผลตรวจ",
        "-" * 78,
    ]
    for r in results:
        verdict = "OK" if r.ok else "MISMATCH: " + ", ".join(
            f"{c}={r.stored[c]:.2f} (ต้องเป็น {r.expected[c]:.2f})"
            for c in r.mismatched)
        lines.append(f"{r.index:>3} {str(r.version or '-'):>7} {r.status:<16} "
                     f"{'yes' if r.executed else 'no':<5} {verdict}")
    bad = [r for r in results if not r.ok]
    frozen = frozen_row_violations(results)
    lines.append("-" * 78)
    lines.append(f"ตรวจ {len(results)} แถว · ผิด {len(bad)} แถว · "
                 f"แถวที่ไม่มี fill แต่ ledger ขยับ {len(frozen)} แถว")
    if frozen:
        lines.append("แถวที่ไม่มี fill แต่ ledger ขยับ (ผิดกฎ execution_confirmed_v1): "
                     + ", ".join(r.run_id or str(r.index) for r in frozen))
    return "\n".join(lines)


def _load_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = [dict(r) for r in csv.DictReader(handle)]
    return [r for r in rows if (r.get(PRICE_COLUMN) or "").strip()]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python lego_cashflow_audit.py <rows.csv> <fix_c> "
              "[p0] [last_action_price] [actual_cumulative]", file=sys.stderr)
        return 2
    rows = _load_csv(argv[1])
    optional = [float(a) for a in argv[3:6]]
    optional += [None] * (3 - len(optional))
    # A CSV export is the 2-dp view on both sides: the prices the expectations
    # are built from, and the stored numbers they are compared against.
    results = audit_rows(float(argv[2]), rows, p0=optional[0],
                         last_action_price=optional[1],
                         actual_cumulative=optional[2],
                         tolerance=DISPLAY_PRICE_QUANTUM,
                         price_quantum=DISPLAY_PRICE_QUANTUM)
    print(format_report(results))
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":                       # pragma: no cover - CLI entry
    raise SystemExit(main(sys.argv))
