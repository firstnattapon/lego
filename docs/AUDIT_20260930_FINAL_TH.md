# Audit 30 กันยายน 2026 — source delivery และ gate ก่อนเปิดเงินจริง

เป้าหมาย: แก้ code ตาม logs และ `implementation_plan_review-6 - final`
แล้วส่ง PR สาธารณะของ source ฉบับเต็ม สำหรับตรวจรับ UAT/PROD แยกกัน
ผู้ใช้ยืนยันว่าไม่มี broker/deployment access หรือหลักฐาน live เพิ่มในรอบนี้
จึงส่ง PR พร้อม gate ที่ค้าง ไม่อ้างว่า incident ถูกแก้ที่ broker หรือเงินจริงพร้อมแล้ว

## หลักฐานและข้อสรุป

ฐาน source คือ `4a8cacd0ad41f15dbf8c8e85652f32b46da17aa2` บน main
ตรงกับ ZIP `lego-main` เมื่อไม่นับ line endings ส่วน ZIP งานรีวิวก่อนหน้า
มี commit `99ebe611de3c751d0f1ba84ead19ef7565038449` ที่ยังไม่อยู่บน main
PR นี้นำเฉพาะการแก้ที่เกี่ยวข้องมารวม ตรวจซ้ำ และแก้ privacy/evidence gaps เพิ่ม

| หลักฐานแนบ | สิ่งที่ตรวจได้ | ข้อจำกัด |
|---|---|---|
| Cloud logs | 1,192 entries, 433 completed ticks; HTTP 200=432, 503=1 | 29 ก.ย. 13:23:04–20:35:05 UTC; ไม่ใช่สถานะปัจจุบัน |
| Business status | RELEASE_EXPIRING=324, MANUAL_RECONCILIATION_REQUIRED=104, EXECUTION_LIMIT_BLOCKED=4, ERROR=1 | HTTP 200/TICK_OK ไม่พิสูจน์ fill |
| RTDB export | 19 intents เป็น SUPPRESSED_ACTIVE_ORDER; positive fills=0 | export ใหม่กว่าช่วง logs; ไม่ใช่ broker scan |
| Offline linkage | 19 rows match; 12 commit events ไม่มี row ที่ตรงใน export ทั้งชุด | หลาย revision/chain; ยังไม่พิสูจน์ duplicate Place หรือสาเหตุการหาย |
| Latest revision | slot เดียวปรากฏต่าง run IDs; พบเพียงรายการหลังใน export | ต้องค้น original chain/archive/backup โดยไม่สร้าง intent สวม |
| Broker blocker | ข้อความระบุ active order ของ UBER | ไม่มี client ID/detail ของ blocker ในหลักฐานเดิม; ยังระบุ ownership ไม่ได้ |

รายงาน offline ของ logs ทั้งชุดตรวจ candidate/revision binding ไม่ผ่าน เพราะมีหลาย
revision อยู่จริง ผลนี้ไม่ใช่ข้อสรุปว่า deployed revision ล่าสุดใช้ source ผิด
ไม่มี inflight fence ใน snapshot ก็ไม่พิสูจน์ว่า broker ไม่มี order
เก็บ raw logs/export/YAML/ข้อมูลบัญชีไว้ private; public evidence ใช้ aggregate และ SHA-256

## สิ่งที่แก้ใน source

- เก็บ open-order witness ทั้งก่อนและหลัง Preview ภายใต้ account-symbol lease
  ที่ยัง valid; ID/status อยู่ใน private outbox/dispatch document
- Public log/response ใช้ fingerprint และเวลาที่สังเกต; ตัด witness ออกจาก public
  audit mirror รวมถึงเส้นทางซ่อม audit; ไม่คัดลอก raw account/token payload
- Idle tick แสดง OPEN_ORDER_BLOCKED จาก observation เดิมโดยไม่อ่าน broker ซ้ำ
  timestamp คือ last observation ไม่ใช่สถานะ fresh ของ broker
- `ops.py inspect-open-orders` อ่านครบทุกหน้าผ่าน SDK v3 แล้วค้น current-chain intent
  ไม่ Place/Cancel/adopt/clear halt หรือเคลียร์ witness; CLI output ต้องเก็บ private
- แยก RELEASE_EXPIRED/DNA_EXHAUSTED จาก warning; ไม่ auto-renew window หรือวน DNA
- Service ที่ใช้ image tag ต้องแนบ matching Ready Revision พร้อม immutable digest
  และ config ที่ตรงกัน; deployment script จับ Revision ให้ receipt/verifier
- Monitoring template ครอบคลุม OPEN_ORDER_BLOCKED/RELEASE_EXPIRED; ยังต้อง apply
  policy และพิสูจน์การส่งถึงผู้รับจริง
- ยกเลิก generator ที่กำหนด PASS IDs/test counts ตายตัวและพึ่งแผนนอก repository
  เครื่องมือใหม่ตรวจ candidate, raw log hashes, exit codes, timestamps, missing/duplicate
  records และเขียน output ใหม่เท่านั้น; local PASS ไม่ทำให้ live acceptance ผ่าน
- Emulator runner เลือก Java/test paths/output ได้ และไม่เขียนทับ JUnit ของ release เก่า

คง guard account+symbol สำหรับทุก open order แม้ไม่พบ intent ของเรา
ไม่ข้าม guard โดยดู client ID prefix ไม่เพิ่ม caps และไม่ blind retry Place/Cancel

## Configuration ที่ขอ

ไฟล์ `deploy/uat-continuous.env.example` มีค่าต่อไปนี้อยู่แล้ว:

```dotenv
WEBULL_ENV=UAT
LEGO_MODE=trade
LEGO_ACTIVE=true
LEGO_SYMBOL=UBER
LEGO_FIX_C=10000
LEGO_ALLOW_FRACTIONAL=true
```

หกค่านี้ยังต้องใช้ร่วมกับ account/secrets, DNA bundle, approved limits/window,
candidate hash และ release authorization ที่ตรงกัน ห้ามใช้ binding ของ release เก่า
Fix_c เป็นเป้าหมายมูลค่าหุ้น ไม่ใช่งบความเสี่ยงของบัญชี
Production ใช้ account/secrets/database/service/binding แยก และไม่คัดลอก UAT caps

## ปิด A1–A6 และ live gates ตามลำดับ

| ข้อ | ขั้นตอนที่ต้องทำกับ environment จริง | หลักฐาน PASS |
|---|---|---|
| A1 pause/resume | `halt-orders` dry-run/apply โดย operator จริง; deploy inactive; คง compatible recovery; ตรวจ inflight ที่ผ่าน admission ก่อน halt | halt scope/ID, inactive Ready Revision, recovery หรือ idle proof |
| Incident/lineage | complete scan หนึ่งครั้ง; อ่าน detail ตาม client ID; ค้น original intent/attempt/policy ใน chain/archive/backup | ownership + account/symbol/side/quantity/broker ID, terminal proof, lineage ของทั้งสอง run |
| Accounting closure | recover intent เดิม; manual eligible ใช้ `tools.resume_order_reconciliation` dry-run และ confirmation จาก fresh proof | actual fills/fees และ ledger ลงครั้งเดียว, ไม่มี unresolved audit/fence |
| A2 quota/migration | ตรวจ market-day counter ก่อน; migrate เฉพาะ legacy ด้วย inactive runtime และไม่มี owner/inflight | before/after + backup hash; conservative count ไม่ refund/reset |
| A3 rollback | halt new orders; ใช้ worker ที่เข้าใจ schema/intent/cancel/audit v4 เดิม; verify revision | unresolved order ยังคง recovery/ledger ได้; ไม่ล้าง fence/state |
| A4 acceptance | ผูก candidate/revision/image/account/config/policy/window กับ artifact hashes และ expected/observed ของทุก gate | ข้อมูลครบใน scope เดียว; missing/UNKNOWN ไม่ใช่ PASS |
| A5 local evidence | ใช้ CI ของ commit หรือ command logs ของ candidate เดียวกัน | install/unit/emulator/compile/dependency/security checks ตาม scope จริง |
| A6 UAT endurance | สอง regular sessions, BUY และ SELL positive fill, cold restart/day rollover; drills unknown Place, partial/cancel-ack, late fee, auth rotation และ rollback | executions/ledgers/holdings/cash/attempt counts ตรง; no duplicate mutation, no missing lineage |
| Monitoring/horizon | ทดสอบ halt/manual/fee-overdue/absent-tick/auth/release/DNA delivery; กำหนด numeric response/recovery objectives | event + generated/received times + operator; token/window/DNA ครอบคลุมช่วงทดสอบ |
| PROD observe | แยก scope, observe/inactive สอง regular sessions, durable verified NORMAL token | authenticated reads/clock/alerts และไม่มี new-order mutation |
| Controlled live | account owner กำหนด quantity/notional/daily attempts/end time/candidate/binding และ rollback | authorization ตรง PROD scope ก่อนส่ง controlled order |
| LIVE_ACCEPTED | actual live fills/fees/positions/cash/ledger/recovery/monitoring ครบ required gates | ขอบเขตที่อนุมัติและ evidence ตรงกัน; มิฉะนั้นคง NO_GO |

คำสั่ง incident เริ่มจาก dry-run ใน **บัญชี/environment เดียวกับ service**:

```bash
python ops.py halt-orders --operator ACTUAL_OPERATOR --reason incident-audit
python ops.py halt-orders --operator ACTUAL_OPERATOR --reason incident-audit --apply
python ops.py status
python ops.py inspect-open-orders > PRIVATE-open-orders.json
```

ผล NOT_FOUND_IN_CURRENT_CHAIN ไม่ใช่ proof ว่า order เป็นของบุคคลอื่น
การ inspect ไม่เคลียร์ persisted witness; dispatch complete scan ที่มี valid lease จึงเคลียร์ได้
ถ้า original intent หายให้คง BLOCKED จนกู้ provenance ที่ตรวจได้ ไม่ auto-adopt/cancel
Cancel acknowledgement ไม่ใช่ terminal/fill/fee proof

ปลด operator halt ได้หลัง closure และ deployed runtime ยัง inactive เท่านั้น
ใช้ halt ID เดิมและผู้ตรวจอีกคนจริงตาม `clear-operator-halt` contract
ไม่ใช้ alias ของคนเดิม ไม่เปลี่ยน status ใน RTDB เพื่อเปิด gate
Initial funding ต้องตรวจรับถ้าอยู่ใน scope; whole-share control ทำหลัง fence ว่าง
และมี binding ของ control แยกจาก fractional candidate

Accounting tolerance: cash ≤ $0.01, quantity ≤ 0.000001 หุ้น
Closing = opening + confirmed executions + explicit external movements
BUY/SELL อาจหักล้างจน cashflow สุทธิเป็นศูนย์และ holdings กลับค่าเดิมได้
อย่าใช้ยอดสุทธิไม่เป็นศูนย์เป็นเงื่อนไข fill acceptance
อนุญาต open orders ระหว่างทางตามปกติ แต่ต้องไม่มี unresolved/stale blocker ณ idle closure

ครบ controlled live scope แล้วหยุดเพิ่ม exposure รอผลรับรอง เว้นแต่ authorization
ครอบคลุมการทำต่อและ required gates ผ่านแล้ว หาก fee/detail ยังไม่ครบคง recovery
หาก identity/arithmetic/safety ผิด ให้ halt และ rollback ตามแผนที่ตรวจแล้ว
DNA/window/token มี horizon จำกัด ต้องเตรียม release ถัดไปพร้อม handover proof
ไม่ reset origin/p0/ledger หรือเปลี่ยน account เพื่อข้าม fence

## ใช้ local evidence ใหม่

```bash
python tools/run_emulator_suite.py --jar PATH_TO_EMULATOR_JAR --java PATH_TO_JAVA_11_PLUS --junitxml NEW_RESULT.xml
python -m tools.build_release_evidence --validation PRIVATE_VALIDATION.json --output NEW_LOCAL_RELEASE.json
```

Validation schema ใช้ `candidate_hash`, `candidate_unchanged` และ `commands[]`
แต่ละ command มี id/argv/exit_code/started_utc/finished_utc/log/log_sha256
raw log paths อยู่ใต้ directory ของ validation file; output ต้องเป็นชื่อใหม่
เครื่องมือพิสูจน์ integrity ของข้อมูลที่ให้ ไม่ใช่ signed attestation หรือยืนยันว่า
operator เลือก commands ครบ scope: reviewer ต้องอ่าน raw output/CI/JUnit ด้วย
ผล local ล่าสุดอยู่ใน `release_evidence/20260930-pr/`; evidence เก่าเป็น historical

## แหล่ง contract ที่ตรวจ

- [Webull llms.txt](https://developer.webull.co.th/apis/llms.txt)
- [Place Order](https://developer.webull.co.th/apis/docs/reference/trade-api/common-order-place.md): QTY รองรับ fractional quantity; CORE/MARKET/DAY ตาม contract
- [Open Orders](https://developer.webull.co.th/apis/docs/reference/trade-api/order-open.md): pagination_key ต้องอ่านครบ
- [Order Detail](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md): identity, filled quantity/price และ actual fee
- [Token](https://developer.webull.co.th/apis/docs/authentication/token.md): verified NORMAL และ reuse จน invalid/expired

**สถานะส่งมอบ:** source/PR เป็นส่วนที่ตรวจได้ในงานนี้; UAT/PROD live gates ข้างต้นยัง BLOCKED
ไม่มีการ deploy, Place, Cancel, เปลี่ยน quota หรือ clear halt ที่บัญชีจริงในรอบนี้
