# LEGO PRINCIPAL v2

20 October audit: [a flight recorder that dropped the ticks that explain an order, a halt that paged every minute, and the real-money gates](docs/AUDIT_20261020_TH.md).
The 19 October UAT session was correct (438 ticks, all 26 rows recomputed from first principles, nothing filled) but the broker failed all 7 UBER SELLs without a reason,
and the flight recorder kept the equation of only 23 of the 26 rows: it wrote a notable tick only with 12 s of tick budget left, and the ticks that matter end because the budget ran out.
Ticks that deferred, failed, committed a row or left an order unresolved are now written down to one second left; a standing broker-reject halt is an `ERROR` for the first 180 s of each
hour of halt instead of on every tick (391 of 438 ticks, replayed: 32); `tools/trace_audit.py` reports skipped ticks, rows without a recorded equation and a log export shorter than the
database; `deploy/continuous-uat.sh` can run the whole-share control (a one-way move for the chain). Decision, ledger, fence and recovery are unchanged. Live acceptance remains **NO-GO**:
alert delivery, real-time PROD quotes, a measured PROD fee and the owner's decision are still open ([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)).

16 October audit: [a standing broker-reject halt, a five-hour broker outage and the real-money gates](docs/AUDIT_20261016_TH.md).
Three UBER SELLs in a row came back `FAILED` with no reason and the sticky `BROKER_REJECT_HALT` then
stopped UAT for the rest of the session. On UAT only, the halt now lets one ordinary READY order through
after a cooldown (900 s, doubling per failed probe, capped at four hours): a reject brings the halt back
at once, a fill clears it. PROD never probes by itself, and nothing configurable switches that on.
`tools/trace_audit.py` now reports a standing halt and long runs of failing decisions. Decision, ledger,
fence and recovery are unchanged. Live acceptance remains **NO-GO**: alert delivery, real-time PROD quotes,
a measured PROD fee and the owner's decision are still open ([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)).

15 October audit: [two clean UAT sessions, remaining alert and real-money gates](docs/AUDIT_20261015_TH.md).
Decision, ledger, fence and recovery are unchanged. Adds webhook coverage for every status the
monitoring health policy pages on, `python ops.py alert-drill` (dry-run unless `--send`),
the Preview-only `tools/measure_fee.py` and a quote-age finding in `tools/trace_audit.py`.
Live acceptance remains **NO-GO**: alert delivery, real-time PROD quotes, a measured PROD fee and the
owner's decision are still open ([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)).

5 October audit: [release horizon, caps and the guarded real-money path](docs/AUDIT_20261005_TH.md).
The 1–4 October UAT stall was not a money-path defect: a 24-hour approval window covered one
session, the DNA (`bypass:500`) was due to end that week and the caps (36,000 USD vs a 10,000 USD
principal) could never bind. `python ops.py release-plan` now checks window, DNA and caps before
every release and prints the values to deploy; renewal alerts fire 48 hours ahead; a PROD live
release needs `LEGO_PROD_LIVE_ACK` bound to its caps and window
([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)). Nothing renews itself and PROD stays observe/inactive
by default. Live acceptance remains **NO-GO** (no UAT BUY/SELL fills or alert delivery proven yet).

30 September final audit: [source changes, evidence and remaining UAT/PROD gates](docs/AUDIT_20260930_FINAL_TH.md).
Adds private broker-blocker diagnostics, Ready Revision digest receipts and
candidate-bound local evidence checks. Live acceptance remains **NO-GO**; local
tests do not prove broker fills, incident closure or production readiness.

Continuous execution v4: [final implementation review](docs/implementation_plan_review.md),
[cutover/operator runbook](docs/CONTINUOUS_RELEASE_V4_TH.md) and
[study guide](docs/LEARNING_CONTINUOUS_V4_TH.md).
This candidate adds one-shot durable cancellation, market-day quotas, release
binding v4 and immutable transition audit. Production remains **NO-GO** pending
live UAT fill/endurance, deployment and alert evidence. Until those gates pass, deploy
Production only in observe/inactive mode (the acknowledged live path is documented in
[PROD_LIVE_RUNBOOK_TH.md](docs/PROD_LIVE_RUNBOOK_TH.md)). The older review below is historical.

23 September readiness hardening: [implementation plan and incident evidence](docs/IMPLEMENTATION_PLAN_20260923_TH.md).
That release required deployment-bound execution limits and a v3 release
binding. Recovery remains enabled with missing/expired limits. The supplied
incident evidence is still **NO-GO for real money** until broker reconciliation
and the operational acceptance gates are closed.

Accounting update: [initial-funding baseline and audit notes](docs/FUNDING_BASELINE_20260916.md).
Broker rejection audit, safety halt and operator runbook:
[22 September 2026 audit](docs/AUDIT_20260922_TH.md).
The v3 accounting writer needs the matching reader update; existing data requires
reviewed reconciliation. Source tests do not constitute production approval.

ระบบ rebalancing แบบหนึ่ง account + หนึ่ง symbol ต่อ environment โดยมี HTTP
entrypoint เดียวคือ `lego_tick`:

`Scheduler → lego_tick → RTDB + Secret Manager + Webull`

deploy code build เดียวกันแยก UAT/PROD อย่างละหนึ่ง Cloud Function รวมสูงสุด 2
functions ไม่มี worker/archive function แยก งานแต่ละ tick ทำตามลำดับ recovery →
decision → dispatch → bounded housekeeping การ pause หยุด intent ใหม่แต่ไม่หยุด
reconcile order ที่เริ่มส่งแล้ว

## Contract หลัก

- 6 operator settings: `symbol`, `principal_usd`, `diff_usd`, `dna_bundle`,
  `mode`, `active`; ค่าเริ่มต้น `observe` + `false`.
- environment/account/endpoint/database/credential/release authorization ผูกกับ
  deployment และ request เปลี่ยนไม่ได้.
- UAT host `th-api.uat.webullbroker.com`; PROD host `api.webull.co.th`; ไม่มี fallback.
- decision `READY_*` ไม่ใช่ fill. Place ถูกเรียกได้ไม่เกินหนึ่งครั้งต่อ intent;
  timeout เป็น `SUBMIT_UNKNOWN` และต้อง reconcile ด้วย client order ID เดิม.
- ledger v2 freeze persisted E จน terminal fill ครั้งถัดไป; `E_mark` แยกจาก E.
- partial fill ขยับ broker cashflow แบบ cumulative delta; model finalize ครั้งเดียว
  ตอน terminal จาก final VWAP.
- 17-column export prefix เดิมยังอยู่; v2 metadata ต่อท้าย.

ดูรายละเอียดที่ [QUICKSTART_TH.md](QUICKSTART_TH.md),
[ADR_LEDGER_V2.md](ADR_LEDGER_V2.md), [STATE_MACHINE.md](STATE_MACHINE.md), และ
[CONFIG_MIGRATION.md](CONFIG_MIGRATION.md).

การแก้ incident fee/audit/deadline และวิธีตรวจ release สำหรับการเทรดต่อเนื่องอยู่ที่
[CONTINUOUS_TRADING_RELEASE_TH.md](docs/CONTINUOUS_TRADING_RELEASE_TH.md)

## Local verification

```powershell
python -m pytest -q
python ops.py check
```

ไม่มีคำสั่ง local ใดใน repo นี้ส่ง order เอง การ deploy, UAT Place และ Production
canary ต้องได้รับ authorization ที่มี environment/account/symbol/side/quantity/วงเงิน
กำกับใน session ที่ลงมือจริงเสมอ
