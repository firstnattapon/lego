# ADR: Bounded FIFO realized ledger v3

Status: accepted for local implementation, 2026-09-06

## Context

The v2 realized ledger bounded its recent idempotency witnesses to 64 records,
but retained every unmatched buy/sell leg inside the authoritative RTDB
transaction subtree. A long one-sided stream therefore made every later
transaction read and rewrite all prior lots. The matched-cycle calculation must
remain FIFO and preserve per-share fee allocation; replacing lots with a
weighted average would change accounting semantics.

## Decision

Keep a bounded authoritative head at `webull_lego_realized/{chain}` and move
each FIFO lot to an immutable direct-key record at
`webull_lego_realized_lot_pages/{chain}/{side}/{sequence}`. Sequence keys are
fixed-width decimal strings. One record is one page in v3; this deliberately
minimizes the transaction/query payload and leaves room to pack records in a
future schema without changing FIFO order.

The head contains only:

- `schema_version`, `ledger_sequence`, and cumulative realized value;
- per-side `fifo_read_cursor` and `fifo_write_cursor`;
- at most one `fifo_head_remaining` partial lot;
- one `active_matching_event_id` and a bounded `matching_progress` checkpoint;
- at most 64 recent event witnesses, plus the existing direct-key archive;
- a bounded `open_legs` projection (at most 16 lots) for compatibility;
- `projection_repair_cursor` and migration metadata.

An incremental broker event is claimed in the head transaction. The operation
stores its immutable input, previous cumulative witness, remaining quantity,
fee per share, accumulated delta, and revision. Matching reads one immutable
opposite-side page outside the transaction, computes its content hash, then a
head transaction verifies event identity, operation revision, cursor, page
sequence, and hash before committing the cursor and realized delta. A page is
never modified or deleted. Partial consumption is represented by the single
bounded `fifo_head_remaining` record. Residual quantity is written with a
create-only page transaction, then linked by a version-checked head transaction.
An orphan page after a crash is harmless and is reused only when its event,
sequence, and content hash match the active checkpoint.

Each call performs at most eight matching steps (`REALIZED_MATCH_PAGES_PER_CALL_V3`).
A step reads one lot page and, when needed, one projection-refill page (at most
16 direct page reads per call); it advances at most one FIFO lot. If
quantity remains, the head keeps the operation checkpoint and the worker leaves
the account-symbol fence in place with `REALIZED_MATCHING_PENDING`; the next
tick resumes. Final event witness, cumulative P&L, cursor/version, and clearing
the active operation occur in one bounded head transaction. Normal RTDB
transaction retries are treated as contention/defer, not permanent math errors.

Unknown broker fees never enter this matched-cycle ledger. Known zero is a real
knowledge state. A later cumulative fee increase applies only its delta, as in
v2. A decreasing cumulative fee remains a reconciliation error because no
authoritative broker correction identity is currently available.

Broker quantity, cumulative average price, fee, notional, and cashflow use
validated Decimal values created directly from broker strings and are serialized
as strings. Float conversion is confined to the legacy matched-cycle/model
projection and never feeds the canonical broker-cashflow calculation.

## Migration

Legacy `open_legs` are copied in FIFO order to immutable pages. The dry run
reports side counts, quantities, fees, and a canonical SHA-256 checksum.
Execution persists `fifo_migration` with source checksum, next index, and target
cursors. Each call copies at most 64 lots using create-only writes and then
advances the checkpoint in a bounded head transaction. Finalization verifies
the unchanged source checksum and copied totals before setting schema v3. A
restart resumes from the checkpoint; rollback before finalization ignores the
unlinked pages, while rollback after finalization requires v3-compatible code
and never restores an RTDB snapshot over broker history.

Migration claims an epoch transactionally before copying. Nonempty legacy
ledgers use `webull_lego_realized_lot_pages/{chain}/generations/{generation}/{side}/{sequence}`;
the v3 head stores `fifo_page_generation`. Existing unnamespaced v3 pages remain
readable when this field is absent. Rollback atomically cancels the epoch and
never deletes pages. A stale writer cannot publish a cancelled checkpoint;
restart uses a fresh generation so orphan pages cannot collide with new source
data. If finalization wins, rollback is refused. Unlinked pages are retained;
physical garbage collection is outside this migration command.

If broker cumulative quantity or fee increases while matching is pending, the
worker finishes the immutable checkpoint first and retains the fence with
`FIFO_NEWER_FACTS_PENDING`. A later invocation applies the additional delta.
The witness always records the checkpoint's actual inputs; model finalization
waits until FIFO has caught up with the latest terminal broker facts. Quantity,
fee, or notional regression continues to fail closed.

## Consequences

History storage grows with lots, but hot head size, direct page reads, each
transaction subtree, and per-call matching work have fixed bounds. FIFO order,
partial fills, cumulative VWAP-derived increments, and fee allocation remain
unchanged. Reader/admin adapters must treat `open_legs` as a bounded projection
and use cursor-aware bounded inspection rather than materializing all history.

