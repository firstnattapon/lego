# Configuration migration: legacy → six operator settings

The only strategy controls in v2 are `symbol`, `principal_usd`, `diff_usd`,
`dna_bundle`, `mode`, and `active`. They are represented by `LEGO_SYMBOL`,
`LEGO_FIX_C`, `LEGO_DIFF`, `LEGO_DNA_BUNDLE`, `LEGO_MODE`, and `LEGO_ACTIVE`.
The first three are required. Defaults are `mode=observe`, `active=false`, and an
explicit finite bypass bundle for a fresh chain.

## Mapping

| Legacy/current name | Class | v2 disposition |
|---|---|---|
| `LEGO_SYMBOL` | operator | KEEP as `symbol` |
| `LEGO_FIX_C` | operator | KEEP as `principal_usd` |
| `LEGO_DIFF` | operator | KEEP as `diff_usd` |
| `LEGO_DNA_CODE` | operator | MIGRATE into bundle; only explicit `bypass:N` may bridge automatically |
| `LEGO_SLOT_SECONDS`, `LEGO_DNA_ORIGIN_UTC`, `LEGO_DNA_CLOCK_MODE` | strategy metadata | MIGRATE into one fingerprinted DNA bundle; deploy mirrors interval/origin for the retained clock |
| `AUTO_SUBMIT` | legacy operator | REMOVE; ignored by typed v2 config and retained only by the legacy façade |
| `LEGO_INLINE_ORDER_WORKER` | orchestration | REMOVE; `lego_tick` always recovers and dispatches in one invocation |
| `LEGO_STRATEGY_ID` | identity | CONSTANT `shannon_demon_lego_v2` for the new chain |
| `LEGO_DECIMAL_PRECISION` | capability | DERIVE from broker `lot_size`; legacy façade only |
| `LEGO_MARKET_CATEGORY` | capability | DERIVE/validate from instrument profile; v2 supports long-only US stocks/ETF represented by `US_STOCK` profile category |
| `WEBULL_ENV` | deployment | KEEP; strict `UAT|PROD`, endpoint allowlist derived |
| `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_ACCOUNT_ID` | secret/deployment | KEEP in Secret Manager bindings; never request JSON |
| `WEBULL_TOKEN_SECRET` | secret/deployment | KEEP as Secret Manager resource name; runtime access only |
| `WEBULL_TOKEN_DIR` | internal | CONSTANT `/tmp/webull_token`; durable truth is Secret Manager |
| `FIREBASE_DB_URL`, `GCLOUD_PROJECT`/`GOOGLE_CLOUD_PROJECT` | deployment | KEEP per environment, not counted as strategy knobs |
| `LEGO_CANDIDATE_HASH`, `LEGO_RELEASE_AUTHORIZATION` | release | KEEP deployment-bound; binding includes environment, account fingerprint, candidate |
| `LEGO_ADMIN_OPERATOR` | admin audit | KEEP only for explicit reconcile CLI |
| `LEGO_ADMIN_RECONCILE_LEASE_SECONDS` | internal | CONSTANT/versioned default |
| `LEGO_ALLOW_EPHEMERAL_TOKEN_DIR` | workaround | REMOVE; Secret Manager hydration resolves the cold-start problem |
| `LEGO_ALLOW_ZERO_HOLDINGS` | unsafe override | REMOVE from v2; missing/vanished positions fail closed |
| `LEGO_ARCHIVE_LIMIT`, `LEGO_ARCHIVE_RETENTION_DAYS` | housekeeping | CONSTANT/versioned default; not user controls |
| `LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING`, `LEGO_DNA_LOW_WATERMARK` | safety | CONSTANT/versioned default; DNA exhaustion is explicit |
| `LEGO_CHAIN_DISPATCH_LEASE_SECONDS`, `LEGO_ORDER_CLAIM_LEASE_SECONDS` | concurrency | CONSTANT/versioned default |
| `LEGO_CLIENT_CACHE_TTL_SECONDS` | performance | CONSTANT/versioned default |
| `LEGO_FILL_CONFIRM_MAX_ATTEMPTS`, `LEGO_RECONCILE_MAX_ATTEMPTS` | recovery | CONSTANT/versioned default; exhaustion never clears broker ambiguity fence |
| `LEGO_HOLDINGS_DRIFT_TOLERANCE` | safety | CONSTANT/versioned numeric tolerance |
| `LEGO_MARKET_EARLY_CLOSES`, `LEGO_MARKET_HOLIDAYS` | calendar | MIGRATE into fingerprinted calendar data/bundle |
| `LEGO_OPEN_ORDER_MAX_PAGES`, `LEGO_OPEN_ORDER_PAGE_SIZE` | broker paging | CONSTANT/versioned bounded defaults |
| `LEGO_ORDER_EXPIRY_MARGIN_SECONDS` | execution | CONSTANT/versioned default |
| `LEGO_TOKEN_REFRESH_MARGIN_DAYS` | auth | CONSTANT; admin rotation alert threshold |
| `LEGO_WEBULL_LOG_LEVEL` | observability | deployment logging level; never strategy behavior |
| `FIREBASE_DATABASE_EMULATOR_HOST` | test | KEEP for emulator only |
| `USERNAME` | local test/tool | REMOVE from production configuration |

## Conflict rules

- `AUTO_SUBMIT` never changes typed v2 `mode` or `active`; only explicit
  `LEGO_MODE` and `LEGO_ACTIVE` control v2 intent creation.
- `LEGO_DIFF` is mandatory; an omitted value is a configuration error, not an
  implicit zero-diff strategy.
- A trained raw DNA without origin/timeframe is rejected; do not guess 15m.
- Request body cannot override host, account, project, database, release binding,
  or symbol.
- Changing P0, DNA fingerprint, operator config hash, account, or environment
  starts a deliberate new v2 chain; it does not splice histories.

## RTDB query-key migration

Before deploying the bounded-query worker over an existing outbox, run
`python tools/backfill_outbox_query_keys.py` as a dry run and then repeat with
`--apply` under the intended admin identity. This is the only full outbox scan;
normal worker ticks query indexed `actionable_sort` and `audit_pending` fields
with server-side limits. A fresh deployment needs no backfill.

## Realized FIFO schema v2 → v3

Schema v3 moves unmatched FIFO lots out of the hot realized head into immutable
direct-key records. It preserves lot order, quantity, price, allocated fee, and
`cumulative_realized`; `open_legs` becomes a bounded compatibility projection.
Read [ADR_BOUNDED_FIFO_V3.md](ADR_BOUNDED_FIFO_V3.md) before operating it.

Run a dry run first. It reports the source checksum, lot/quantity/fee totals,
checkpoint, and remaining records without writing:

```powershell
python tools/migrate_realized_fifo_v3.py CHAIN_KEY
```

After export/checksum and with old intent creation disabled, copy one bounded
batch at a time and repeat until `complete=true`:

```powershell
python tools/migrate_realized_fifo_v3.py CHAIN_KEY --apply --max-pages 64
```

Before finalization only, the migration epoch can be cancelled while the intact
legacy `open_legs` remains authoritative:

```powershell
python tools/migrate_realized_fifo_v3.py CHAIN_KEY --rollback --confirm
```

After schema v3 finalizes, rollback is refused. Use code that understands v3;
never restore an old RTDB snapshot over broker executions.
