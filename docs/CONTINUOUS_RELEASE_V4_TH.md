# LEGO continuous execution v4 — คู่มือ cutover และตรวจรับ

สถานะการส่งมอบโค้ดแยกจากการอนุมัติเทรด: local tests ไม่รับรอง UAT endurance หรือบัญชีเงินจริง ดูผลล่าสุดใน `release_evidence/continuous-v4-acceptance.json` และ `docs/implementation_plan_review.md`.

## Policy ที่ใช้

UAT profile อยู่ใน `deploy/uat-continuous.env.example`: UBER, FIX_C=10000, DIFF=25, fractional=true, trade/active, slot=900s, DNA bundle `strategy.uat-continuous.json` (`bypass:6500`, origin เดิม, จบ 2027-09-07T19:30Z), 26 attempts ต่อ market day (= จำนวน slot ต่อ session), quantity cap=30 และ estimated notional cap=1,500 USD (15% ของ principal) ไม่ใช่วงเงิน Production. ค่าเดิม 30/1000/36000 ไม่เคย bind (order จริงสูงสุด ~86 USD) จึงถูกแทนที่ — ที่มาและวิธีเลือก cap ดู `docs/AUDIT_20261005_TH.md`. DNA ใหม่ = `config_hash`/chain ใหม่ (strategy ไม่เปลี่ยน) และเริ่มจาก holdings จริงของบัญชี.

| Config | ค่าเริ่มต้น runtime | UAT profile |
|---|---|---|
| `LEGO_STALE_ORDER_ACTION` | `hold` | `cancel` |
| `LEGO_STALE_ORDER_SECONDS` | 300 | 300 |
| `LEGO_CANCEL_CONFIRM_GRACE_SECONDS` | 120 | 120 |
| `LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER` | 1 | 1 |
| `LEGO_SESSION_KEY_MODE` | `release_window` (legacy compatibility) | `market_day` |
| `LEGO_MAX_SESSION_ORDERS` | ต้องระบุเมื่อเปิด new orders | 26 |
| `LEGO_MAX_ORDER_QUANTITY` / `LEGO_MAX_ORDER_NOTIONAL_USD` | ต้องระบุเมื่อเปิด new orders | 30 / 1500 |
| `LEGO_TRADING_WINDOW_END` | ต้องระบุเมื่อเปิด new orders | เวลาที่เจ้าของบัญชีอนุมัติใหม่ |

`LEGO_ORDER_TIMEOUT_SECONDS` เป็น configuration error พร้อมคำแนะนำย้ายค่า ไม่มี alias ที่เปลี่ยน policy เงียบ ๆ. Internal budget คง 35s และ Cloud Run timeout 45s. หลัง Place อ่าน detail ทันทีได้ไม่เกินหนึ่งครั้ง จากนั้น tick ถัดไปรับงานต่อ. Safe deadline deferral ตอบ 200/TICK_DEFERRED; configuration/persistence failures ยังคง 5xx. Auth circuit ไม่กลบ persistence failure ให้กลายเป็น success.

## คำสั่งค้างและการลงบัญชี

Intent ใหม่เก็บ cancel policy/hash, candidate, release binding และ worker schema 4. Intent เก่าไม่มี policy ใช้ hold เสมอ. การแก้ env เป็น inactive/observe หรือ release หมดอายุหยุด new orders แต่ยัง reconcile และใช้ cancel policy ที่บันทึกไว้กับ intent เดิมได้.

เมื่อ broker ยืนยัน non-terminal ที่รู้จักและยังเหลือ quantity: ตรวจ client ID, symbol, side, total quantity, broker ID ที่ทราบ และ account เมื่อ response มีข้อมูล พร้อมตรวจ account/environment fingerprint ของ worker. อายุเริ่มจาก durable `placed_at` ของ place-attempt witness. ถ้าไม่อาจพิสูจน์ข้อมูลจะคง fence และส่งให้ตรวจด้วยคน.

ครบ 300s ต้องเป็นเจ้าของ intent lease และ account-symbol fence จึง transaction บันทึก `CANCEL_REQUESTED`, count=1, requested time และ confirmation deadline ก่อนเรียก SDK. Crash หลัง witness ใช้สิทธิ์ไปแล้วแม้ยังไม่ได้ส่ง HTTP. Timeout/error/response สูญหายเป็น `CANCEL_UNKNOWN`; ไม่มี cancel retry และไม่มี replacement Place. Cancel acknowledgement ยังไม่ใช่ terminal proof.

หากยังไม่ terminal หลัง grace 120s จะตั้ง manual flag และ operator halt. ระบบยัง query เพื่อรับ late fill/fee แต่ไม่ยกเลิก halt เอง. History fallback ใช้เมื่อ detail ล้มเหลว/UNKNOWN/หลักฐานไม่ครบ อ่าน grouped v3 pages จนครบภายในช่วงย้อนหลังไม่เกิน 7 วัน. Cursor ซ้ำ, pagination ไม่ครบ, identity ขัดแย้ง หรือไม่พบ order คง fence. เกิน 7 วันต้อง manual reconciliation.

Zero-fill cancel ไม่สร้าง cashflow. Partial fills ใช้ cumulative evidence ลง broker cashflow แบบ delta; terminal fill จึง finalize model ledger ตาม semantics เดิม และต้องยืนยัน holdings. Fee ที่มาช้าแก้เฉพาะส่วนต่าง. การปลด fence ต้องผ่าน terminal, model/realized/cashflow/fee/holdings guards และเขียน audit ที่ค้างสำเร็จ. ห้ามลบ fence/outbox เพื่อให้ระบบเดินต่อ.

## ขั้นตอนก่อนและระหว่าง cutover

1. ปิด new orders บนทุก service/worker ที่ใช้ account-symbol เดียวกัน. หยุด Scheduler สำหรับ cutover และรอ requests/leases เดิมจบ. เก็บ read-only broker orders/history/positions/cash/fees ปัจจุบัน พร้อม RTDB backup ใหม่ในพื้นที่ private.
2. ตรวจ unresolved fence, operator halt และ pending audit ผ่าน `python ops.py status`. ต้อง reconcile ให้ครบก่อน migration. Export วันที่ 26 กันยายนไม่รับรองสถานะปัจจุบัน.
3. ใช้ v4 candidate ใน observe/inactive. รัน `python ops.py migrate-market-day` ซึ่งเป็น dry-run และตรวจยอดที่เสนอ. เมื่อ live broker และ fence ว่างจึงใช้ `python ops.py migrate-market-day --apply`. คำสั่ง apply ปฏิเสธเมื่อ runtime อนุญาต new orders หรือ fence/owner ยังอยู่; malformed counters ถูก block. เก็บ output ทั้งสองครั้งกับ backup hash.
4. Counter ใช้ `XNYS:YYYY-MM-DD` จาก market clock นิวยอร์ก แยก release expiry. Migration ยกยอดอย่างอนุรักษนิยมด้วยค่าสูงกว่าระหว่าง legacy/daily; reservation นับก่อน Place และไม่ refund เมื่อ cancel/crash. ห้ามเปลี่ยน release/DNA/config เพื่อคืน quota วันเดิม.
5. ตรวจ candidate manifest, revision/image digest, config hash, secret versions และ runtime service account. ห้ามให้ worker รุ่นก่อน v4 รับ traffic หรือ tagged URL หลัง cutover. ลบ old revision tags/ปิด old scheduler endpoints และตรวจว่าไม่มี worker เก่ารันอยู่ก่อนเปิดใหม่. Runtime v4 ปฏิเสธ intent schema ที่ใหม่กว่าตน; marker นี้ไม่อาจทำให้ binary รุ่นเก่าที่มีสิทธิ์ admin เรียนรู้ schema ใหม่ได้ จึงต้องบังคับที่ rollout/traffic/IAM ด้วย.
6. สร้าง binding ด้วย `python ops.py release-binding` ภายใต้ env/profile ที่จะใช้จริง. Deploy script เรียก `config.release_binding_for` ตัวเดียวกัน. Binding v4 ครอบคลุม account/environment/candidate/symbol/strategy/DNA/fractional/limits/session/cancel policy. v3 binding เปิด new orders ไม่ได้.
7. วางแผน release ด้วย `python ops.py release-plan` (ดูหัวข้อ "ต่ออายุ release UAT" ด้านล่าง) แล้วใช้ `deploy/continuous-uat.sh` เฉพาะเมื่อ cutover หลักฐานครบ. Wrapper กำหนด UAT policy ที่ตกลงและต้องมี EXPECTED_CANDIDATE_HASH, LEGO_RELEASE_AUTHORIZATION_OVERRIDE, LEGO_TRADING_WINDOW_END_OVERRIDE ซึ่ง release-plan พิมพ์ให้ครบ. Deploy script รัน release-plan ซ้ำก่อน deploy trade/active และไม่ deploy เมื่อมี BLOCK (window หมด/สั้นกว่าสอง session, DNA จบก่อน window, caps ใช้ไม่ได้). Deploy script มี smoke invocation ซึ่งอาจ Place ใน trade/active ที่ได้รับอนุญาตแล้ว.
8. ตรวจ actual settings ที่ deploy: max instances=1, concurrency=1, timeout=45, Scheduler retry=0, cadence ทุกนาที, traffic 100% ไป revision เดียว ไม่มี tag ไป worker เก่า. `tools/verify_deployment.py` ตรวจ captured JSON แบบ read-only. เก็บผลและ source captures แบบ private.

```bash
gcloud run services describe lego-tick-uat --region=asia-southeast1 --format=json > private-service.json
gcloud scheduler jobs describe lego-tick-uat --location=asia-southeast1 --format=json > private-scheduler.json
gcloud functions describe lego-tick-uat --gen2 --region=asia-southeast1 --format=json > private-function.json
python tools/verify_deployment.py --service private-service.json --scheduler private-scheduler.json --function private-function.json --candidate HASH --revision REVISION --image REGISTRY/IMAGE@sha256:DIGEST
```

หาก service capture ระบุ image เป็น tag ยังตรวจ image digest ไม่ผ่าน ต้องเก็บ revision/image provenance จริงเพิ่มเติม ห้ามแทนด้วย hash ที่เดา. ห้าม commit captures ที่มี environment secrets.

## ต่ออายุ release UAT (ก่อนหมด ≥ 48 ชม.)

Window, DNA และ token หมดอายุเงียบ ๆ ได้ (เหตุการณ์ 1–5 ตุลาคม: window 24 ชม. ครอบ session เดียวแล้ว order ถูกบล็อกสี่วัน) จึงไม่มีอะไรต่ออายุเอง และทุก release ต้องผ่าน `release-plan` ก่อน deploy. จาก checkout ที่สะอาดของ commit ที่ตรวจแล้ว:

```bash
python ops.py status                         # ไม่มี inflight, operator halt หรือ fence ค้าง
PLAN="$(gcloud secrets versions access latest --secret=webull-account-id-uat --project=lego-firebase \
  | python ops.py release-plan --env-file deploy/uat-continuous.env.example --account-id-stdin \
      --window-sessions 10 --enforce)"
jq -r '.assessment.findings[] | select(.severity != "INFO") | "[\(.severity)] \(.id): \(.message)"' <<<"$PLAN"   # หยุดเมื่อมี BLOCK
export EXPECTED_CANDIDATE_HASH="$(jq -r .deploy_env.EXPECTED_CANDIDATE_HASH <<<"$PLAN")" \
       LEGO_RELEASE_AUTHORIZATION_OVERRIDE="$(jq -r .deploy_env.LEGO_RELEASE_AUTHORIZATION_OVERRIDE <<<"$PLAN")" \
       LEGO_TRADING_WINDOW_END_OVERRIDE="$(jq -r .deploy_env.LEGO_TRADING_WINDOW_END_OVERRIDE <<<"$PLAN")"
ALERT_WEBHOOK_SECRET_OVERRIDE=<secret> bash deploy/continuous-uat.sh
```

- `--window-sessions N` สิ้นสุดที่ปิดตลาดของ session ที่ N (หรือ `--window-end 2026-10-16T20:00:00Z`); ต้องครอบ ≥ 2 session สมบูรณ์
- `release-plan` ไม่เขียน/ไม่ส่งอะไร และไม่พิมพ์ account id; `--enforce` exit 1 เมื่อ BLOCK: window หมดแล้ว/สั้นกว่าสอง session,
  DNA จบก่อน window, caps อ่านไม่ได้. WARN (เช่น notional > 25% ของ principal, orders > slot ต่อ session) ต้องอ่านและตัดสินใจ
- `--reference-price <ราคา> --recommended-limits` แสดง caps ที่คำนวณจาก principal (UAT: notional 15%, qty 1.3 เท่า, orders = slot ต่อ session) เทียบกับ policy file
- release ใหม่ทุกครั้งต้องคำนวณ binding ใหม่ (window end อยู่ใน policy hash) — ห้ามนำ `LEGO_RELEASE_AUTHORIZATION` เก่ามาใช้
- DNA ปัจจุบัน (`bypass:6500`) จบ 2027-09-07: ต้องเปลี่ยน bundle (chain ใหม่) ก่อนถึงวันนั้น ดู `docs/PROD_LIVE_RUNBOOK_TH.md` ข้อ 8; release-plan บล็อกเมื่อ DNA จบก่อน window

## Token และ Production observe

Production profile `deploy/prod-observe.env.example` ใช้ PROD/observe/inactive และเว้น monetary limits/expiry ไว้ให้อนุมัติแยก. เส้นทางเงินจริงเป็น release แยกที่ผ่าน acknowledgement (`deploy/prod-canary.env.example`, `docs/PROD_LIVE_RUNBOOK_TH.md`) และยัง NO-GO. Runtime ต้อง hydrate durable token secret ที่ ready ก่อนสร้าง PROD SDK client จึงไม่เริ่ม interactive OTP ระหว่าง cold start. ก่อน new orders ยังต้องผ่าน authenticated account/market-data/capability/positions/buying-power/open-orders reads เดิมครบ.

Token invalid/expired หรือ Secret Manager unavailable ให้ปิด new orders และตรวจ alert. ผู้ดูแลออก/ยืนยัน token ผ่าน Webull workflow ที่รองรับ แล้วนำไฟล์ token รูปแบบ token/expiry/NORMAL ไปยังพื้นที่ private. ใช้ `python ops.py bootstrap-auth --help` เพื่อดู dry-run/apply สำหรับเพิ่ม secret version; ไม่มี token value ใน command line หรือ PR. เปลี่ยน version ที่ deployment ใช้, cold restart และพิสูจน์ authenticated read ใหม่. Token lifecycle อาจต้องยืนยันตัวตนอีกครั้ง ไม่ต่ออายุอัตโนมัติจากการมี Secret Manager เพียงอย่างเดียว.

## Cloud Monitoring และ audit

```bash
python tools/monitoring_config.py --service lego-tick-uat --channel projects/PROJECT/notificationChannels/CHANNEL_ID --output-dir private-monitoring
```

คำสั่งนี้ render JSON เท่านั้น: log metric ของ `lego_tick_completed`, log-match health policy (severity ERROR, AUTH_BACKOFF, OPERATOR_HALT, OPEN_ORDER_BLOCKED), horizon policy (release/DNA/token — rate limit 6 ชม. และ auto-close 24 ชม. เพื่อไม่ page ทุก tick ระหว่างรอต่ออายุ) และ missing ticks 180s (3 รอบของ cadence 60s). ผู้ดูแลสร้าง metric/policies ใน Cloud Monitoring จากไฟล์เหล่านี้แล้วตรวจ notification channel ปลายทางจริง. Webhook ไม่บังคับ แต่ถ้าต้องการให้ release/DNA/halt alert ถึงคน ให้เก็บ URL ปลายทางใน Secret Manager แล้วตั้ง `ALERT_WEBHOOK_SECRET_OVERRIDE=<ชื่อ secret>` ตอน deploy (script ผูกเป็น `ALERT_WEBHOOK_URL` และให้สิทธิ์ runtime service account; URL อาจมี credential จึงไม่ใส่เป็น env value). ต้องมี tick เพื่อเริ่ม time series ก่อนทำ absence drill; metric ที่ไม่เคยมีข้อมูลไม่ใช่หลักฐานว่าทดสอบ absence แล้ว.

Health ใช้ business status และ severity ไม่ใช้ HTTP 200 เป็นหลักฐานปกติ. ครอบคลุม manual halt, cancel unresolved ที่เกิน grace, reconciliation overdue, ledger/fee anomaly, token/release/DNA warning และ tick absence. ทดลอง halt/resume, secret invalid, missing tick และบันทึก incident ID/time/received time/channel โดยไม่ใส่ข้อมูลบัญชีลับ. Warning DNA เปรียบเทียบ remaining slots กับสอง sessions ถัดไปและรายงาน end time ตาม calendar; ไม่วน DNA และไม่ต่อ release เอง. `RELEASE_EXPIRING` เตือนล่วงหน้า 48 ชม. (พร้อม `release_sessions_remaining`); `RELEASE_EXPIRED` และ `DNA_EXHAUSTED` เป็น severity NOTICE (สถานะที่รู้อยู่แล้ว ไม่ใช่ WARNING ทุก tick) แต่ webhook ส่งเวลาสิ้นสุดจริงหนึ่งครั้งต่อวัน (cooldown durable 24 ชม. ต่อ kind+scope).

Private path `webull_lego_execution_transitions/{chain}/{run}/r_{revision}` เป็น immutable event. State transaction เก็บ pending event แล้ว replay ด้วย transaction ที่ห้ามทับข้อมูลเดิม ก่อน mutation ถัดไป. Event มี intent/transition revision, candidate, deployment revision, release binding และ state fields ที่อนุญาต. Mirror failure ไม่ทิ้ง pending; backlog เต็มหยุดเพื่อซ่อม. Rules ปฏิเสธ public/reader access; Admin SDK ยังต้องคุม IAM แยก. ไม่บันทึก credentials หรือ raw broker payload ใน telemetry.

## รายงานรายวัน

```bash
python tools/daily_execution_report.py --export private-export.json --broker-evidence private-broker.json --output private-daily-report.json
```

Broker evidence เป็น normalized private snapshot ที่ได้จาก authenticated reads ไม่ใช่ raw payload. Closing และ `opening` ต้องมี `captured_at` (timezone), `environment`, `account_fingerprint`, `orders_complete=true`, `positions_complete=true`, `positions` (symbol → decimal quantity), `cash` (cash balance USD ชนิดเดียวกันทั้งสองจุด ไม่ใช่ buying power), `orders` (client_order_id, symbol, side, status, filled_quantity, filled_price, filled_fee). ให้รวม order identities ที่ export ใช้อ้างอิงและยอด cumulative ไม่ใช่ transaction delta.

Closing เพิ่ม `opening`, `external_cash_delta` และ `external_position_delta` (symbol → quantity) ซึ่งต้องระบุ explicit แม้เป็น 0/{}. รวม deposits/withdrawals/transfers/corporate actions ที่ยืนยันได้. Opening/closing ห่างไม่เกิน 48h และบัญชี/environment ตรงกัน. Comparison ตรวจ orders/fills/fees, broker ledger, model/realized witnesses, positions/cash delta (tolerance 0.000001 หุ้น/0.01 USD) และ fence/audit. ข้อมูลไม่ครบ BLOCKED/FAIL ตามชนิดข้อผิดพลาด ไม่ตั้งค่าหายเป็น zero. ถ้า hot witness ถูก archive/prune ต้องแนบหลักฐาน retained archive ให้ครบก่อนใช้ผลรายงาน.

PASS ของ daily snapshot คือ reconciliation ของข้อมูลที่ส่งเข้ามาเท่านั้น. `real_money_ready` ยังคง false. ต้องตรวจ arithmetic/row lineage เพิ่มด้วย `tools/readiness_audit.py` จาก logs/export ของ candidate เดียวกัน และตรวจ source captures ว่าเป็นข้อมูลจริงครบถ้วน.

## ตรวจรับและ rollback

Local: `python -m pytest -q` จะ skip emulator เมื่อไม่ได้ตั้งค่า. Full gate ใช้ `python tools/run_emulator_suite.py --jar PATH_TO_FIREBASE_DATABASE_EMULATOR.jar` (Java ต้องอยู่ใน PATH) ซึ่งสร้าง emulator บน localhost, โหลด rules, รันทุกรายการ แล้วปิด process ของตน. ไม่มี broker calls จริงใน suite. CI มี unit gate และ emulator gate แยกกัน.

UAT ต้องผ่านสอง regular sessions ครบ พร้อม cold restart/day rollover, fractional profile, BUY และ SELL positive fill, initial funding และ ledger closure, fault injection ไม่มี duplicate Place/ledger/fence release ผิด, alert delivery และไม่มีการปลด fence ด้วยมือระหว่าง endurance. Whole-share control ทำแยกหลัง fence ว่าง; fractional=false อย่างเดียวไม่รับประกันเกิด whole-share order. Cancel-only หรือ UAT ไม่มี fill ให้ BLOCKED.

PROD observe/inactive อย่างน้อยสอง regular sessions ต้องมี authenticated reads, clock/token/monitoring evidence และไม่มี new-order mutation. การเปิดเงินจริงต้อง release authorization ใหม่พร้อมวงเงินต่อคำสั่ง, daily order count, approval window และ `LEGO_PROD_LIVE_ACK` ที่ผูกกับ release นั้น (runtime ปิดเองถ้าไม่มี/ไม่ตรง). ไม่ใช้ UAT caps เป็นค่าเริ่มต้น. ขั้นตอนและเงื่อนไขอยู่ใน `docs/PROD_LIVE_RUNBOOK_TH.md`.

Rollback: ปิด new orders แล้วคง worker ที่เข้าใจ cancel/audit schema v4 เพื่อ reconcile ต่อ. ไม่ย้อนกลับไป binary ที่ไม่รู้จักสถานะใหม่ ไม่ลบ outbox/fence ไม่ refund quota และไม่ clear operator halt อัตโนมัติ.

อ้างอิง: [Webull API index](https://developer.webull.co.th/apis/llms.txt), [Cancel Order](https://developer.webull.co.th/apis/docs/reference/trade-api/common-order-cancel.md), [Order History](https://developer.webull.co.th/apis/docs/reference/trade-api/order-history.md), [Token lifecycle](https://developer.webull.co.th/apis/docs/authentication/token.md), [Cloud Monitoring AlertPolicy](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies).
