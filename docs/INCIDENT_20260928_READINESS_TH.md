# ผล audit วันที่ 29 กันยายน 2026

ตรวจ source main `2012f486ecbd0d806c349e01bc74f4636a1af197`, log, RTDB export,
และ implementation_plan_review-1 ถึง -4 ที่ผู้ใช้ส่งมา

**ส่งมอบโค้ดสำหรับตรวจรับ UAT/PROD แต่ยังไม่รับรองว่าบัญชีจริงพร้อมเปิดเทรด**
หลักฐานที่ให้มายังมี order ที่ส่งแล้วแต่กระทบบัญชีไม่ครบ ต้อง resolve กับ broker
ก่อนเริ่ม order ใหม่ ห้ามล้าง outbox/fence เพื่อให้ audit ผ่าน

## หลักฐานที่ยืนยันได้

- 1,409 log records; 422 ticks และ 422 HTTP requests ที่จับคู่กันได้
- 391 ticks เป็น MANUAL_RECONCILIATION_REQUIRED, 30 RELEASE_EXPIRING, 1 ERROR
- outbox 3 รายการ: SUPPRESSED_STATE_CHANGED 2 และ PLACING_UNKNOWN 1
- unresolved fence 1, operator halt 1; positive fill ที่พิสูจน์ใน outbox ได้ 0
- HTTP 200 เป็นสถานะ transport ไม่ใช่หลักฐานสำเร็จในการเทรด
- log SHA-256: `7bdd1d1015d4c5556925fb499e48cd7e4680f2227d8519c7c782b2a785af9dc8`
- RTDB SHA-256: `9def9cd462f28b85ab4bdf28faf034349e7813c2cef6955511b2380d39394656`

เหตุการณ์ส่ง BUY 147.13455 หุ้นจบที่ `order quantity changed` แต่ไม่ได้เก็บ raw
Order Detail ตอนเกิดเหตุ จึงยังแยกไม่ได้ว่าเป็น schema/alias หรือจำนวนจริงผิดกัน
ไม่อ้างว่า fixture ใหม่เป็น response ที่จับจาก UAT จริง และไม่เพิ่ม quantity tolerance
การที่ holdings ขยับตรงจำนวนสั่งเป็นข้อมูลประกอบ ไม่ใช่หลักฐานระบุตัว order

## สิ่งที่แก้และสิ่งที่มีอยู่แล้ว

| ข้อสังเกต | ผลในโค้ด |
|---|---|
| broker quantity contract | CanonicalOrderEvidence ใช้ Decimal, รองรับ total_quantity/quantity/qty/order_quantity และ filled_quantity/filled_qty; ปฏิเสธค่าขัดกันระหว่าง alias/wrapper |
| immutable submitted quantity | ตรวจ payload กับ intent และ broker แบบตรงค่า; FILLED ต้องครบ total; เก็บ broker ID จาก Place ACK ก่อนอ่าน Detail |
| conflicting/incomplete evidence | แยก exception; ข้อมูลขาดใช้ history ภายใต้ budget เดิม; จำนวน/identity ขัดกันหยุดและเก็บ diagnostic แบบ private พร้อม digest |
| manual halt read churn | MANUAL_RECONCILIATION_REQUIRED เป็น queue-terminal; รองรับ flag needs_manual_check ของข้อมูลเก่า; ก่อนสร้าง SDK client ต้องตรวจ fence |
| row semantics | ขณะ operator halt หรือ recovery fence ค้างให้ PASS_* / NO_ACTION / quantity=0 โดยไม่มี intent ใหม่; DNA observation ยังเดินได้ |
| accounting | ใช้ cumulative/idempotent broker cashflow และ model/realized finalizer ที่มีอยู่; ไม่ถือ position delta เพียงอย่างเดียวเป็น fill |
| safe recovery | tools.resume_order_reconciliation อ่าน terminal proof ใหม่ แล้ว re-arm เฉพาะ reconciliation ภายใต้ lease; ไม่ส่ง/ยกเลิก order และไม่ปลด halt/fence |
| token lifecycle | Secret Manager hydration ที่มีอยู่เพิ่ม cache permission 0600; PROD ตรวจ Check Token โดยตรง ต้อง NORMAL พร้อม expiry/identity ที่ตรวจได้; ใช้ live expiry กับ new-order floor |
| quote freshness | UAT allowance คงอยู่เฉพาะ UAT; PROD quote <=60s และ decision <=120s; overshoot และ future-skew guard คงอยู่ |
| observability | tick ที่พัก reconciliation เป็น INFO heartbeat แต่ business_status ยัง unhealthy; alert dedup/retry แบบ durable ที่มีอยู่ยังทำงาน |
| release provenance | deploy จาก clean Git checkout, ส่ง LEGO_GIT_COMMIT และบันทึก receipt ที่ผูก commit/candidate/image digest/revision/environment/risk/account-secret binding โดยไม่ overwrite |

Receipt เป็นหลักฐาน binding ของการ deploy ที่ capture มา ไม่ใช่ signed build attestation
หรือ broker acceptance; account-secret fingerprint ไม่ใช่หลักฐานว่าบัญชีจริงตรงจนกว่าจะผ่าน read-only gate

## การตรวจออฟไลน์

ใช้ pytest ทั้งชุด, regression ของ fractional/nested evidence, quiet halt, explicit recovery,
PROD quote/token, และ Firebase emulator สำหรับ rules กับ concurrent money-path probes
ตรวจ dependency ด้วย `python -m pip_audit -r requirements.txt --progress-spinner off`
รายงานผลรันจริงและ CI อยู่ใน PR ไม่ใช้เอกสารนี้แทนผลทดสอบ

รัน audit หลักฐานเดิมด้วย tools.readiness_audit แล้วได้ BLOCKED / real_money_ready=false
ตามที่ควรเป็น ไม่แก้ export ต้นฉบับและไม่อัปโหลด raw log/RTDB/Cloud Run secrets ไป public

## การใช้งานและ gate ที่เหลือ

ค่าที่ต้องการสำหรับ UAT:

```sh
WEBULL_ENV=UAT
LEGO_MODE=trade
LEGO_ACTIVE=true
LEGO_SYMBOL=UBER
LEGO_FIX_C=10000
LEGO_ALLOW_FRACTIONAL=true
```

หกค่านี้ยังไม่ครบ deployment contract ต้องมี credential binding, DNA bundle,
candidate/release authorization และ explicit quantity/notional/session/time-window limits
ตาม deploy/uat-continuous.env.example ไม่ต่ออายุ trading window อัตโนมัติ

1. Pause scheduler และรอ worker lease เดิมหมดก่อนแก้ incident
2. ตั้ง environment/account/symbol ของ incident เดิม แล้วรันจาก repo root:

   ```sh
   python -m tools.resume_order_reconciliation --chain CHAIN --run RUN --operator REVIEWER
   ```

   เป็น dry run อ่าน broker เท่านั้น หากข้อมูลขัดกันยังต้องตรวจ broker/support
   ห้ามกรอก fill ขึ้นเอง หาก terminal proof ผ่าน ให้ตรวจผลและรันซ้ำพร้อม
   `--confirm 'RESUME HASH_FROM_DRY_RUN'`; command จะอ่าน broker ใหม่และปฏิเสธหาก evidence เปลี่ยน

3. เปิดเฉพาะเส้นทาง recovery ของ worker ให้ ledger finalizers ทำงานจน outbox,
   broker cashflow, model/realized ledger และ audit witnesses ตรงกัน
   operator halt ยังคงอยู่ หาก crash ให้ตรวจ durable state; ไม่ rerun แบบเดาสถานะ
4. ใช้ admin reconciliation/ops ที่มีอยู่ตรวจและปลด halt หลัง fence resolved แล้วเท่านั้น
5. เก็บ UAT BUY/SELL/partial/cancel/reject/ambiguous Place/restart evidence และ soak
   เต็ม intended market session ไม่มี duplicate Place หรือบัญชีไม่ตรง
6. PROD เริ่ม `LEGO_MODE=observe LEGO_ACTIVE=false`: ตรวจ endpoint/account,
   NORMAL token, positions/cash, real-time quote, alerts, source/image receipt
7. ก่อนเงินจริง ต้องกำหนดและอนุมัติ canary budget/window เฉพาะ แล้วตรวจ fill,
   fees, position/cash และ ledger ให้ครบก่อนเพิ่ม limits

Continuous หมายถึงทำงานอัตโนมัติภายในช่วงเวลาที่อนุมัติและ token ที่ยัง valid
Token 2FA/rotation, unresolved broker conflicts และการขยายวงเงินยังต้องมีผู้ดูแล
MARKET order มี pre-trade notional guard แต่ไม่รับประกัน execution price

## Webull contract ที่ตรวจ

- [Order Detail](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md): total_quantity เป็นจำนวน submitted, filled_quantity เป็น cumulative execution
- [Order History](https://developer.webull.co.th/apis/docs/reference/trade-api/order-history.md): secondary lookup ภายใต้ client order ID เดิม
- [Check Token](https://developer.webull.co.th/apis/docs/reference/trade-api/check-token.md): NORMAL/PENDING/INVALID/EXPIRED
- [เอกสารรวม](https://developer.webull.co.th/apis/llms.txt)
