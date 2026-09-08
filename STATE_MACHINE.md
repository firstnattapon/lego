# V2 execution state machine

Allowed lifecycle:

`INTENT_CREATED → PREVIEWED → PLACE_ATTEMPTED → SUBMITTED | SUBMIT_UNKNOWN`

`SUBMITTED | SUBMIT_UNKNOWN → PARTIALLY_FILLED | TERMINAL_RECONCILING | NEEDS_RECONCILIATION`

`PARTIALLY_FILLED → PARTIALLY_FILLED | TERMINAL_RECONCILING | NEEDS_RECONCILIATION`

`TERMINAL_RECONCILING → DONE | NEEDS_RECONCILIATION`

`TERMINAL_RECONCILING → REALIZED_MATCHING_PENDING → TERMINAL_RECONCILING`

`REALIZED_MATCHING_PENDING` means the terminal broker fill spans more than the
fixed per-tick FIFO page budget. Its immutable page/cursor checkpoint resumes on
the next worker invocation; the account-symbol fence stays held.

`INTENT_CREATED | PREVIEWED → UNSENT_ABORTED` only before a place-attempt marker.

## Invariants and failure recovery

| Failure point | Durable evidence | Recovery | May call Place again? |
|---|---|---|---|
| before intent commit | no intent | next eligible slot | no replay of missed slot |
| after intent commit | immutable intent/run ID | materialize outbox | yes, once, after all gates |
| after attempt marker, before/inside network | marker + stable client ID | exact-ID detail/open/history reconciliation | no |
| broker accepted, response lost | SUBMIT_UNKNOWN | exact-ID reconciliation; fence retained | no |
| nonterminal partial fill | cumulative broker facts | apply only qty/notional/fee deltas | no |
| terminal before model commit | terminal facts + intent | idempotent finalization transaction | no |
| FIFO page write/head checkpoint/projection interruption | immutable page hash + active event/cursor/revision | resume at most 8 pages per tick; never resubmit | no |
| model commit before projection | finalized run witness/seq | repair projection only | no |
| pause/observe/DNA exhausted/market closed | attempted intent remains actionable | reconcile/finalize; create no new intent | no new mutation |
| unresolved after bounded reads | NEEDS_RECONCILIATION | audited operator evidence | no |

Lease expiry never deletes the attempt marker. A negative detail lookup once is
not proof of absence. Manual/shared-account drift blocks the account-symbol fence.
