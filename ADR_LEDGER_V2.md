# ADR: execution-terminal-frozen-v2

Status: accepted by the supplied implementation contract.

Legacy rows retain `execution_confirmed_v1`. The new `shannon_demon_lego_v2`
chain writes schema version 2 and `execution_terminal_frozen_v2`.

For observation price `Pₙ`, genesis `P₀`, principal `C`, persisted model `A`,
last terminal fill `P_acted`, and persisted frozen excess `E`:

`R_market = C × ln(Pₙ/P₀)` and `E_mark = A − R_market`.

Without a new terminal model finalization: `ΔA=0`, and `A`, `P_acted`, and `E`
are copied exactly. `E_mark` may move and never overwrites `E`.

At a terminal nonzero fill with final cumulative VWAP `P_f`:

`ΔA = C × (P_f/P_acted − 1)`; `A_next=A+ΔA`;
`P_acted_next=P_f`; `E_next=A_next−R_basis`, where `R_basis` is the immutable
`R_market` from the originating decision. A monotonic `finalized_seq` and the
intent ID make this exactly-once inside RTDB.

Partial fills update actual cashflow incrementally while nonterminal. A final
FILLED/CANCELLED/FAILED/EXPIRED with nonzero cumulative quantity finalizes the
model once from final VWAP. Zero-fill terminal leaves the model frozen.

Broker cashflow is separate:

`Δqty=CumQty−PrevCumQty`; `Δnotional=CumQty×Avg−PrevCumNotional`;
`cash_delta = sign×Δnotional−Δactual_fee`, with BUY sign −1 and SELL sign +1.
Fees are never guessed from Preview.

The worked examples and fee-only correction are executable in
`test_v2_contract.py`. E and A are model statistics, not guaranteed profit.

