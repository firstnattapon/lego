# LEGO PRINCIPAL v2

25 October audit: [two UAT sessions that traded continuously, complete records, a dispatch that spent its last seconds on reads the place fence would refuse, and the real-money gates](docs/AUDIT_20261025_TH.md).
On 22-23 October revision 00055 (UBER) placed 21 orders and 13 filled, with no halt and no outage; 3,908 ticks matched 3,908 requests and all 52 rows were recomputed from first principles
without a mismatch. One tick (22 October 14:15Z) had 8.1 s left before the gate yet still ran Preview and two broker reads, ended with 0.575 s, lost its flight-recorder trace (row v4's equation)
and was logged as an ERROR because the post-Preview refetch swallowed the deadline. The gate now asks for the fence's 12 s before its first read and the refetch passes the deadline on;
on the log's 25 Previews no placed order is affected. Three BUYs stuck `PENDING` on the UAT broker cost 6 slots and three BUYs `FAILED` without a reason (the broker sends none);
both go to Webull and the 30-minute UAT release is unchanged. UAT fees (~1.08% per order) put every order below break-even. Live acceptance remains **NO-GO**: alert delivery,
real-time PROD quotes, the 2FA token path, a measured PROD fee and the owner's decision are still open ([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)).

22 October audit: [a UAT quote guard that blocked every order of the day because the sandbox feed ran hours behind the clock, logs that could not say why, and the real-money gates](docs/AUDIT_20261022_TH.md).
On 21 October no order reached the broker: all 22 `READY_BUY` decisions (6 UBER, 16 AAPL) ended `SUPPRESSED_STATE_CHANGED` with `quote_age_limit`, because the sandbox snapshot fell 2.5 to 5.9 hours
behind the clock (13:40Z at 16:10Z, 13:52Z at 19:45Z) against a 1,260 s allowance; NFLX and MSFT are not tradable on UAT, and the AAPL deploy was an initial-funding release (2 orders per session).
Data and equations were correct (903 ticks, 16 rows recomputed from first principles, nothing filled, no fence held). On UAT a quote now has to come from the open session instead (the previous
close still fails) at all five dispatch checks; PROD keeps its 60 s. The tick log now names the guard reasons and which instrument check refused, `tools/trace_audit.py` reports
`DISPATCH_SUPPRESSED`, and the continuous UAT policy is UBER again (steady caps 30 / 1,500 / 26). Live acceptance remains **NO-GO**: alert delivery, real-time PROD quotes,
the 2FA token path, a measured PROD fee and the owner's decision are still open ([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)).

21 October audit: [a dispatch gate that dropped both BUYs of the session on a transient `balances/get` 417, a decision commit that never checked the time left, and the real-money gates](docs/AUDIT_20261021_TH.md).
The 20 October UAT session was correct (437 ticks, all 26 rows recomputed from first principles, nothing filled) but the broker failed all 5 UBER SELLs without a reason (15 in a row since 16 October),
and both BUY intents ended as `NOT_PLACED` with 14 minutes of their slot left: `balances/get` answered HTTP 417 three times and the gate treated a transient read as a verdict on the order.
A transient read before Place now defers the intent at most twice (every guard runs again on each try), a tick that runs out of time while reading is no longer turned into `NOT_PLACED`,
the decision stage defers the whole tick when less than 3 s is left after the snapshot (row v2 was committed with 0.88 s left and has no recorded equation), and the UAT token check counts a client pair
that is still valid (a false `webull_token` warning at 16:45:07Z). Decision, ledger, fence, recovery, order limits and the circuit rules are unchanged. Live acceptance remains **NO-GO**:
the broker failure, alert delivery, real-time PROD quotes, a measured PROD fee and the owner's decision are still open ([runbook](docs/PROD_LIVE_RUNBOOK_TH.md)).

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
