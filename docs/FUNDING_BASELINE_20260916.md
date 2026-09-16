# Initial funding and continuous-runtime audit — 2026-09-16

Status: source changes prepared for review; **not production certified**. No
broker orders, production deployment or live database edits were performed.
Private input exports and repair plans are not part of this public repository.

## Defect and accounting contract

A flat account's first BUY was finalized as an ordinary rebalance. For example,
a principal of 5,000, decision quote 27.34 and first fill VWAP 27.37 booked
`5000 * (27.37 / 27.34 - 1) = 5.48646671543529` into delta, cumulative model
return and excess. No position existed during that price interval.

The new `execution_terminal_funding_v3` writer initializes new flat v2-strategy
chains with an explicit `initial_funding_zero_v1` policy. Only the first confirmed
funding BUY may consume that marker, inside the existing state transaction:

- `delta_A = A = E = 0`, `P_acted = actual fill VWAP`, finalized sequence = 1.
- Unfilled/rejected decisions and DNA clock steps do not consume the marker.
- A later terminal fill uses `FIX_C * (fill / previous_fill - 1)` normally.
- The market reference `R_market` retains its original observation origin.
  If funding happens later, store `funding_reference_offset = R_market` at the
  funding decision. Subsequent `R_basis = R_market - funding_reference_offset`;
  finalized `E = A - R_basis`. On funding itself `R_basis = 0`.
- Frozen observation rows retain their model A/E. Broker cash, actual fees,
  filled shares and FIFO realized P&L are separate and are never zeroed.

This is a model return ledger, not broker profit: it scales returns by FIX_C,
not actual invested quantity, and excludes actual fees. R also follows quoted
prices while A follows fill prices; E is not guaranteed nonnegative.

Existing positions retain their previous behavior. Existing finalized ledgers
are carried forward and are **not silently rebased**. Versioning the accounting
semantics makes an old frozen-v2 writer reject an upgraded chain, protecting
against rollback/mixed writers. Deploy the updated dashboard first: older
readers may reject the new semantics until upgraded. Strategy identity, lot
rules, DNA, principal and chain key do not change.

## Historical data correction

`tools/plan_initial_funding_repair.py` is an offline planner for a deliberately
narrow case: complete versions 1..N, first row BUY from flat, exactly one terminal
fill, no further broker attempts, matching model mirrors and broker notional,
and no active dispatch lock. It refuses missing history, extra fills, active or
ambiguous orders and inconsistent data. It does not connect to any service.

```bash
python tools/plan_initial_funding_repair.py PRIVATE_EXPORT.json CHAIN_KEY --output PRIVATE_PLAN.json
```

The plan contains the canonical whole-export hash, before-values and proposed
leaf updates across rows, state, order audit and outbox. Broker cashflow, fee,
FIFO, quantity, price, run IDs, DNA steps and decision versions remain intact.
`apply_to_copy` verifies the plan against an in-memory copy; it is not a live
migration executor. A changed source invalidates the plan.

Before applying any correction: pause **all** writers including recovery workers,
settle any outstanding broker order, make a fresh private backup/export, re-plan
against that export, validate the exact hash and before-values, and apply only
the reviewed leaf changes atomically with a privileged maintenance process.
Keep the backup and plan as audit evidence. Never import an old full export into
a live database. Resume only the new writer after checking all projections.
Multiple-fill histories need a separately reviewed full replay.

## Further audit findings

- The dashboard's frozen-v2 check compared persisted values to an unchanged copy,
  so corrupted delta/excess could pass. The companion reader change independently
  verifies fill provenance, previous execution basis, funding and frozen rows.
  It checks raw persisted data before display recomputation and recognizes v3.
- A retry after the bounded finalized-run history expired reconstructed R_basis
  from R_market and lost finalized_seq. The row fallback now preserves the
  original basis, sequence and funding provenance without applying cashflow twice.
- Migration messages now distinguish carrying an existing execution baseline
  from resetting a pre-execution ledger; they name the actual target semantics.
- A dispatch deadline may expire before Place. Recovery must recheck price and
  holdings next tick, and may safely suppress the stale intent. A suppressed
  SELL is not a SELL fill. Deadline and duplicate-order fences remain enabled.

## Verification and release limits

Regression tests cover nonzero DNA genesis step, fill slippage, delayed first
funding, later rebalances, existing positions, legacy continuity, state/row
crash recovery, evicted-history replay, downgrade protection and repair refusal.
`tools/emulator_funding_probe.py` races eight workers against real local RTDB:
exactly one finalization applies and all model funding columns remain zero.
The existing rules matrix and 16-worker dispatch/tick probes remain in CI.

Local verification: **797 backend tests passed, no skips**, in an isolated
Python 3.12 environment with the pinned requirements and local RTDB emulator;
**70 companion reader tests passed**. All 80 Python source/test/tool files
parsed successfully and dependency consistency (`pip check`) passed. The
private one-fill export failed the new reader check before correction and its
offline corrected copy passed, without modifying broker/FIFO data.

Offline tests and a UAT snapshot do not establish production readiness. Release
requires one identifiable candidate deployed to UAT, fresh data reconciliation,
actual BUY and SELL lifecycle evidence (including partial/cancel/late-fee cases),
multi-session soak and restart evidence, production account/quote/token/fee
verification, deployed IAM/rules and alert checks, and an approved live order
budget. The target asset value is not an all-in cash spending cap: fees are extra.
Finite DNA must be renewed through the established controlled process; this
change does not loop or extend it automatically.

References checked: [Webull API index](https://developer.webull.com/apis/llms.txt),
[global Order Detail](https://developer.webull.com/apis/docs/reference/order-detail.md),
[Thailand Order Detail](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md),
[Open Orders](https://developer.webull.com/apis/docs/reference/order-open.md).
The detail contract distinguishes executed quantity, average fill price and
actual collected fees; none should be inferred from a decision or preview.
