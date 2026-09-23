# Implementation plan และผล audit — 23 กันยายน 2026

สถานะ: **NO-GO สำหรับเงินจริงจากหลักฐานที่มี**. PR นี้ส่งมอบโค้ดและการทดสอบเพื่อปิดช่องโหว่ที่พิสูจน์ได้ ไม่ได้ deploy, ส่งออเดอร์, ปลด fence หรือรับรองผลตอบแทนของกลยุทธ์

## ข้อวินิจฉัยต่อร่างเดิม

เห็นด้วยกับ recovery ก่อน decision และ fail-closed เมื่อไม่รู้ผล Place แต่ไม่เห็นด้วยกับการใช้ principal เป็นเพดาน order, การถือว่า HTTP 200 เป็นหลักฐาน fill หรือการถือว่า tests ผ่านแล้วเท่ากับพร้อมเงินจริง ต้องแยกความพร้อมของโค้ดออกจากสถานะ broker ปัจจุบัน

ร่างเดิมระบุให้รออนุมัติแผนก่อนเริ่ม implementation; คำขอ full code/PR ครั้งนี้อนุญาตให้แก้โค้ดและทดสอบแล้ว ส่วน live UAT/PROD ยังต้องมี account/symbol/วงเงิน/ช่วงเวลาที่ระบุในรอบลงมือจริง

## หลักฐานและผลตรวจ

ใช้ GitHub base `e861cd7` และตรวจเทียบ ZIP ใน workspace หลัง normalize line endings พบ local changes เดิมใน migration audit, tests และ QUICKSTART. นำ migration fix พร้อม tests มารวมโดยไม่ใช้ ZIP ทับโค้ดล่าสุด

| หลักฐาน | SHA-256 |
|---|---|
| Cloud log export | `ee9fd836ddd08eaac40c8adaefea266590a1e1fa504334eff6ba57df569f9670` |
| RTDB export | `7f76adc434c8190bc041cfd4022c8f75c6055046662ad411ed7ec16e47e59092` |

Raw logs/export มีข้อมูลบัญชีและ broker จึงไม่รวมใน PR; รวมเฉพาะรายงาน aggregate ที่ redacted ไว้ใน `release_evidence/ACCEPTANCE.json`. รายงานนั้นประเมินหลักฐาน incident เดิม ไม่ใช่หลักฐานว่ามีการ deploy PR นี้แล้ว

| ข้อค้นพบ | ผลกระทบ/การแก้ |
|---|---|
| 15 intents: FILLED 1, RECONCILE_ABANDONED 1, EXPIRED_UNSENT 12, SUPPRESSED_STATE_CHANGED 1 | abandoned เคย Place; ต้องยืนยันผล broker ด้วย client ID เดิมก่อนปลด fence |
| Mirror ตรง outbox 15/15; fill ที่พบมี fee/cashflow/model/realized witness | พิสูจน์ snapshot ภายในเท่านั้น ไม่ใช่ยอดบัญชี broker ปัจจุบัน |
| Money fence ค้าง 1 จุด | รักษา fence และ recovery; ไม่ reset อัตโนมัติ |
| 421 requests และ 421 tick events; 391 WAITING_RECONCILIATION | จำนวนเท่ากันไม่ได้พิสูจน์หนึ่งต่อหนึ่ง; event เดิมไม่มี Cloud trace |
| Log 2026-09-22 13:30–20:30 UTC; export มี update ถึง 2026-09-23 00:34 UTC | ไม่ใช่ snapshot เวลาเดียวกัน ห้ามตีความว่าหายจาก log = ไม่มีเหตุการณ์ |
| migration tool บน main อ่าน `webull_lego_outbox` เก่า | แก้ให้อ่าน current/legacy, block เมื่อ malformed/missing/manual/attempted/fenced และ exit 1 |
| Recovery ตรวจ source row ก่อน broker แม้เคยส่งแล้ว | ย้าย broker reconciliation มาก่อน source-row gate; source สูญหายห้ามแปลง attempted เป็น NOT_PLACED |
| ไม่มี deployment order/session cap | เพิ่ม cap แบบ Decimal และ transactional session reservation |

## งานที่ implement

1. **Cutover audit:** ตรวจทั้งสอง schema และ active fence; conservative policy ยังคง block แม้ FILLED ที่เคยส่งแล้ว เพราะการเปลี่ยน writer ต้อง review execution history แยก ไม่ได้แปลว่า FILLED นั้นผิด ห้ามลบ outbox เพื่อให้ gate ผ่าน
2. **Readiness report:** `python -m tools.readiness_audit` ตรวจ schema, mirror revision/status/fill, cumulative cashflow arithmetic, model/realized witness, duplicate client IDs, request/trace pairing และ candidate/revision ทุก event. Missing evidence ไม่เป็น PASS; CLI คืน 1 พร้อม BLOCKED. เป็น snapshot audit ไม่ใช่ immutable transition journal
3. **Request audit:** ใส่ Cloud Logging trace/span จาก `X-Cloud-Trace-Context` ที่ตรวจรูปแบบแล้ว ใช้ project จาก deployment; ไม่ log headers/payload อื่น. เพิ่ม execution-limit และ reconciliation-age fields
4. **Execution limits:** ตรวจ quantity และ estimated notional ก่อน Preview และอีกครั้งด้วย quote หลัง Preview; ตรวจ deadline อีกครั้งหลัง transaction. ไม่ตัด quantity เงียบ ๆ เพราะจะทำให้ Preview/Place ต่างจาก decision
5. **Session count:** reserve ใน transaction ของ account-symbol dispatch lock ก่อน irreversible Place. Retry run เดิมไม่เพิ่ม count; cold start, code release, เปลี่ยน cap หรือสะกด timezone ต่างกันใน window เดิมไม่ reset count. Crash หลัง reserve อาจใช้ slot โดยไม่ได้ Place: ตั้งใจให้ปลอดภัยและไม่คืน slot อัตโนมัติ. Window ใหม่ที่สิ้นสุดภายหลังเริ่ม session ใหม่ได้เฉพาะหลัง operator เปลี่ยน config/binding; ห้ามใช้เพื่อหลบ budget ที่อนุมัติ
6. **Release binding v3:** ผูก environment/account/candidate/symbol/execution-limits. v2 binding ใช้เปิดส่งใหม่ไม่ได้. Missing/invalid/expired limits ไม่ขวางการ recover ออเดอร์เก่า
7. **Alert:** known SUBMITTED/PARTIAL ที่ค้างอย่างน้อย 15 นาทีมี `RECONCILIATION_OVERDUE` แม้ broker read สำเร็จ. แจ้ง fee overdue/manual reconciliation/limit blocked ผ่าน webhook เดิมเมื่อ config ไว้ มี timeout และ durable dedup 24 ชั่วโมง; failed delivery retry ได้หลัง 60 วินาที. ไม่เปลี่ยนผล broker หรือปลด fence ตามเวลา
8. **Deploy:** `deploy/uat-observe.sh` ต้องระบุ full reviewed commit และ clean checkout, เริ่ม observe/inactive. PowerShell และ Cloud Shell ส่ง cap เข้า runtime และตรวจ config สำหรับ trade+active; PROD template ว่างเพื่อ block ส่งใหม่โดยปริยาย

## การตั้ง execution limits

| Environment variable | ความหมาย |
|---|---|
| `LEGO_MAX_ORDER_QUANTITY` | จำนวนหุ้นสูงสุดต่อออเดอร์ เป็น finite decimal > 0 |
| `LEGO_MAX_ORDER_NOTIONAL_USD` | quantity × ราคาจาก quote ที่ผ่าน freshness gate สูงสุด เป็น finite decimal > 0 |
| `LEGO_MAX_SESSION_ORDERS` | จำนวน reservation สูงสุดของ account-symbol ใน window เป็น integer > 0 |
| `LEGO_TRADING_WINDOW_END` | สิ้นสุด window เป็น ISO-8601 พร้อม timezone; ถึงเวลานี้ block order ใหม่ |

Cloud Shell ใช้ชื่อข้างต้นต่อท้าย `_OVERRIDE`. PowerShell รับ `-MaxOrderQuantity`, `-MaxOrderNotionalUsd`, `-MaxSessionOrders`, `-TradingWindowEnd`.
สร้าง authorization ใหม่ด้วย `python ops.py release-binding` หลังตั้ง env ของ candidate/account/symbol/limits ตรง deployment จริง (เป็น deterministic deployment acknowledgement ไม่ใช่ cryptographic approval จากบุคคลที่สาม)

Notional cap เป็น **วงเงินประเมินก่อนส่ง MARKET** ไม่รับประกันราคาหรือวงเงินจริงหลัง slippage และไม่รวม fee. หากต้องการ hard execution-price bound ต้องออกแบบและทดสอบ LIMIT/price collar แยก ห้ามเรียก cap นี้ว่า guaranteed cash ceiling. Principal และ diff ยังคงเป็นค่ากลยุทธ์ ไม่ใช่ risk budget

ไม่กำหนดตัวเลข cap จริงแทน operator และไม่เปิด session โดยอัตโนมัติเมื่อหมด window. การใช้งานต่อเนื่องต้องกำหนด window/cap ตามช่วงปฏิบัติการและมีผู้รับผิดชอบต่ออายุ authorization

## ขั้นตรวจรับที่ต้องทำต่อใน environment จริง

1. คง observe/inactive; ดึง log/export ที่เวลาเดียวกัน พร้อม revision/config/account fingerprint. ตรวจ abandoned client ID จาก order detail, open/history orders และ trade events; หากยังคลุมเครือให้ broker ยืนยัน. ใช้ manual reconciliation workflow ที่มีหลักฐาน ไม่ clear fence ตรง ๆ
2. Pin reviewed commit, รัน CI และ Firebase Emulator, เก็บ manifest จาก clean checkout ที่ใช้ deploy จริง. หลีกเลี่ยงการคัดลอก candidate hash จาก Windows checkout ที่เปลี่ยน line endings ไปใช้ Linux. Hash report ใน `release_evidence/` ถูก exclude จาก candidate เพื่อไม่เกิด self-reference
3. Deploy UAT observe/inactive; ตรวจ candidate/image/revision/account/endpoint/config, quote timestamp, token, buying power, holdings, open orders และ exact Preview. ตั้ง cap/window ที่ได้รับอนุญาตก่อน trade+active
4. UAT BUY และ SELL ที่ fill จริง, partial/cancel/reject, Place timeout/UNKNOWN, cold start, paused recovery, late fee, limit exhaustion, alert success/failure; soak อย่างน้อยหนึ่ง session ตาม workload ที่ตั้งใจใช้. ปิดยอด broker cash/holdings/fees เทียบ ledgers และเก็บ transition evidence ทั้งช่วง
5. PROD ใช้ identity/binding ใหม่, observe/inactive, read-only smoke และตรวจ zero Place. กำหนด alert owner, heartbeat monitor ที่ทำงานแม้ function ไม่ถูกเรียก, token renewal, RPO/RTO และ rollback drill. จากนั้นจึงอนุมัติ canary ที่ระบุขอบเขตชัดเจน

GO ต้องมีหลักฐานทุก gate; snapshot tool นี้จงใจไม่ออก GO แทน operational sign-off. ผล 1 fill หรือ HTTP 200 อย่างเดียวไม่ผ่านเกณฑ์

## Reproduce แบบไม่ส่งออเดอร์

```bash
python -m pytest -q -rs
firebase emulators:exec --only database --project demo-lego-firebase "python -m pytest -q test_database_rules.py"
python -m pip_audit -r requirements.txt --progress-spinner off
python -m tools.migration_audit /private/export.json
python -m tools.readiness_audit --export /private/export.json --logs /private/logs.json --candidate VERIFIED_HASH --revision DEPLOYED_REVISION --output release_evidence/ACCEPTANCE.json
```

Regression tests ใช้ broker doubles เท่านั้น; emulator probes ใช้ local RTDB และไม่มี Webull calls. รายละเอียดผลตรวจจริงดู `release_evidence/VALIDATION.json`

## เอกสารอ้างอิงที่ตรวจรอบนี้

- [Webull official index](https://developer.webull.co.th/apis/llms.txt): ดาวน์โหลดได้โดยตรงในรอบนี้
- [SDK และ environment](https://developer.webull.co.th/apis/docs/sdk.md): UAT/PROD hosts แยกกัน
- [Order detail](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md): query ด้วย account และ client_order_id เดิม, ID ไม่เกิน 32 ตัวและ unique ต่อ account
- [Stock order lifecycle](https://developer.webull.co.th/apis/docs/trade-api/stock/): Preview/Place แล้วจึงตรวจ order detail
- [Trading FAQ](https://developer.webull.co.th/apis/docs/trade-api/faq.md): client identity และ trade events
- [Token lifecycle](https://developer.webull.co.th/apis/docs/authentication/token.md): production 2FA และเงื่อนไข token invalid เมื่อไม่มี API calls 15 วัน; ไม่สมมติว่า token มีอายุคงที่ 15 วัน
