# M0 Baseline — 2026-09-05

## Provenance

- Working target: `lego-firebase-main`, a source snapshot (not a Git checkout).
- Verified comparison checkout: `../lego-firebase-upstream`.
- Upstream remote: `https://github.com/firstnattapon/lego-firebase.git`.
- Comparison commit: `1e555310277442a232b2f4efde7bfb3dc54c994a` (`codex/fix-webull-response-contracts`).
- At task start the principal snapshot hashes matched the supplied framework:
  `main.py` `8ac352...4b8`, `lego_one_row.py` `d47d61...056`,
  `lego_state.py` `b4d87d...53e`, `lego_orders.py` `682093...6e6`,
  `lego_outbox.py` `0b62f3...e5f`, `webull_io.py` `531348...a58`.
- Framework attachment SHA-256 is retained by the Codex task; the complete
  approved contract was parsed before any edit.

## Fresh pre-change evidence

- Command: `python -m pytest -q`
- Result: `644 passed, 1 skipped in 9.74s`.
- This result is characterization only. It is not release evidence for the v2
  candidate and is never reused as a live/cloud PASS.
- No `AGENTS.md` existed in readable workspace paths. `.review-envs` was access
  restricted and was not modified.

## Initial architecture and risk inventory

- Three HTTP-decorated functions: decision, order worker, archive worker.
- 38 environment names were observed in the original production Python files;
  they mixed user controls, deployment identity, safety constants, and test/admin
  values.
- Existing durable intent, claim-generation, place-attempt marker, chain dispatch
  fence, cumulative realized-fill dedupe, and terminal partial-fill handling were
  retained as high-value safety mechanisms.
- Existing cashflow semantics were `execution_confirmed_v1`; v2 is a new strategy
  chain and does not relabel legacy rows.

## Source-of-truth rule

The release manifest hashes the edited snapshot itself. A Git commit is not
invented for this non-Git directory. Cloud inventory, UAT lifecycle, Production
observe, and Production canary remain separate evidence gates.

