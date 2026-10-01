# Audit 1 ตุลาคม 2026 — snapshot outage และการเทรดต่อเนื่อง

เป้าหมาย: ตรวจหลักฐานใหม่ แก้ source ที่จำเป็น และส่งโค้ดครบชุดสำหรับ PR
ใน `firstnattapon/lego` โดยคงการตรวจรับ UAT และเงินจริงแยกกัน
ฐานคือ `572767eec6721c1b686efc147aadc8d76043848a` ซึ่งตรงกับ upstream HEAD
ขณะเริ่มงาน ไม่มีการส่ง Place/Cancel หรือ deploy ในงานนี้

## หลักฐานที่ตรวจได้

| ไฟล์แนบ | SHA-256 | สิ่งที่ยืนยันได้ |
|---|---|---|
| downloaded-logs-20261001-204445.json | `035a0ba0757655a0ca93c1cee43bb8ddb0ca90db0bd605a0889497fc2e1163be` | 3 entries: Scheduler เริ่ม 1 ครั้ง และจบด้วย HTTP 503 จำนวน 2 ครั้ง |
| lego-firebase-default-rtdb-export.json | `36e4f4ac100f8c83a0ef3f023e8f1e886a38473f06221aa438cb6ff97cdd52c2` | error เดียวจาก snapshot HTTP 500 INTERNAL_ERROR และ dispatch lock ที่ owner/lease ว่าง |
| webull_uat_read_probe.py (แนบสองครั้ง) | `8705f5b70690f716e7395d7b59a41759068a08967df648b78aa54a2b68e1a866` | ทั้งสองไฟล์เหมือนกัน เก็บ source หนึ่งชุดใน `tools/webull_uat_read_probe.py` |

ช่วงหลักฐาน Scheduler คือ 1 ตุลาคม 2026 เวลา 20:43:21–20:44:28 น.
Asia/Bangkok; เป็นข้อมูลในอดีต ไม่ใช่สถานะ broker ปัจจุบัน
ไม่ publish raw logs/export, URL deployment, account data หรือ claim token

**Expected:** อ่านราคาและ holdings ที่ตรวจได้ แล้วประเมิน slot; เมื่อ broker ขัดข้อง
หยุดสร้างออเดอร์ใหม่ชั่วคราว โดยยังตรวจสถานะและลงบัญชีออเดอร์เดิมได้

**Actual:** SDK snapshot โยน HTTP 500 หลัง safe-read retry; decision ตอบ 503
ทำให้ Scheduler เรียกอีกครั้ง ไม่พบ row/outbox/fill ใน export ที่แนบ
การไม่มีข้อมูลเหล่านั้นใน export นี้ไม่พิสูจน์ว่า broker ไม่มีออเดอร์หรือเคยเทรดสำเร็จ
lock ที่ owner/lease ว่างเพียงอย่างเดียวไม่ใช่ deadlock และไม่มีเหตุให้ล้าง fence

**Hypothesis ที่หลักฐานรองรับ:** snapshot route ขัดข้องสำหรับ request นี้;
โค้ดยังไม่มี durable pause สำหรับ market-data outage และตัวกรอง diagnostic
ไม่ยอมเก็บ `INTERNAL_ERROR` สาเหตุฝั่ง Webull (symbol, entitlement, environment,
หรือ backend) ยังแยกไม่ได้จาก 500 เดียว จึงไม่เปลี่ยน endpoint/symbol หรือใช้ราคาเก่าแทน

## การแก้ source

- Managed `lego_tick` ใช้ snapshot หนึ่ง attempt แล้วบันทึก cooldown ใน private RTDB
  แยก environment/account/symbol/category: 60, 120, 240, 480, 960 และสูงสุด 1800 วินาที
- เมื่อ cooldown ผ่าน ใช้ transaction ให้หนึ่ง request ถือ probe lease 45 วินาที
  การ cold start ไม่ล้าง cooldown; request ที่ lease หมดอายุไม่สามารถล้างหรือยืดสถานะใหม่
- ตรวจ pause ก่อนสร้าง clients/capability/holdings สำหรับ decision;
  ทุก snapshot ที่ dispatch เรียกยังต้องผ่าน circuit เดียวกัน
- จะคืน quote ได้เมื่อ price finite/positive และมี broker trade time ที่อ่านได้เท่านั้น
  guard อายุราคาและเงื่อนไขก่อน Place เดิมยังทำงาน ไม่มี stale quote fallback
- `MARKET_DATA_BACKOFF` ตอบ HTTP 200 เพื่อให้ Scheduler รับทราบการพัก
  โดย business status/log severity เป็น WARNING พร้อมเวลา probe/cooldown และ
  allowlisted HTTP status, code, operation, request ID; HTTP 200 ไม่ใช่หลักฐาน fill
- การตรวจ status/fill/fee ของออเดอร์ที่ส่งไปแล้วไม่ผ่าน market-data circuit
  operator halt, ambiguous-order fence, execution limits และ release binding เดิมยังบังคับใช้
- เพิ่มสถานะนี้ใน monitoring template และ optional webhook rate limiter เดิม
  ยังไม่ได้ apply monitoring หรือยืนยันการส่งแจ้งเตือนใน cloud
- Read-only probe ที่ผู้ใช้ให้ใช้ standard library และไม่ส่ง order/preview/cancel
  ไม่มี credentials จึงตรวจได้เฉพาะ source/CLI; ยังไม่ได้เรียก Webull จริง

เส้นทาง legacy/CLI ที่ไม่มี managed tick context คง safe-read retry เดิมไว้;
durable circuit เป็นส่วนของ scheduled `lego_tick` ซึ่งมี Firebase และ request budget
อายุ lease นานกว่า budget ของ managed tick 35 วินาที การเปลี่ยนไปใช้ worker
ที่ไม่มี tick context ต้องตรวจ behavior นี้แยกก่อน deploy

## Configuration และการตรวจรับ

ค่าหกตัวที่ขอมีใน `deploy/uat-continuous.env.example` แล้ว:

```dotenv
WEBULL_ENV=UAT
LEGO_MODE=trade
LEGO_ACTIVE=true
LEGO_SYMBOL=UBER
LEGO_FIX_C=10000
LEGO_ALLOW_FRACTIONAL=true
```

ยังต้องตั้ง account/app credentials/token, immutable DNA bundle, limits/window,
candidate hash และ authorization ที่ตรง environment; หกตัวนี้ไม่เปิด trading gate ทั้งหมด
PROD ใช้ `WEBULL_ENV=PROD` (`api.webull.co.th`), แยก account/secrets/database/release
จาก UAT (`th-api.uat.webullbroker.com`) และใช้ PROD observe/inactive ก่อนตรวจรับเงินจริง
ไม่คัดลอก UAT risk caps ไปใช้ PROD

ลำดับขั้นต่ำก่อนเปิดใหม่:

1. ใช้ read-only probe กับ credentials UAT ที่ถูกต้องครั้งเดียว เก็บ output private;
   ตรวจ config/account/positions และ quote ของ UBER/controls พร้อม request IDs
   500 ทุก symbol ยังไม่ยืนยัน entitlement หรือ root cause; 403 จึงเป็นหลักฐาน access denial
2. ถ้า snapshot ยังผิด ให้ Webull ตรวจ request IDs และ scope; เปลี่ยน hypothesis ตาม
   response ใหม่ ไม่เรียก probe เดิมซ้ำโดยไม่มีข้อมูลหรือ state ใหม่
3. Deploy candidate แบบ inactive พร้อม rules ที่ปิด public read/write ของ circuit;
   ตรวจ Ready Revision และ release binding ของ candidate ใหม่ ห้ามใช้ binding ของ source เก่า
4. เปิด UAT ใน scope ที่อนุมัติและตรวจ actual BUY/SELL fills, fees, positions/cash/ledger,
   restart, recovery และ alert delivery ตาม `docs/AUDIT_20260930_FINAL_TH.md`
5. ตรวจ PROD observe และ controlled live scope แยก; ต้องมี durable verified NORMAL
   token และหลักฐาน actual live fill/accounting/recovery ก่อนเรียก LIVE_ACCEPTED

## ข้อจำกัดของงานนี้

ผลตรวจ source ฉบับสุดท้าย: pytest 1,306 PASS / 9 SKIP / 0 FAIL;
9 SKIP เป็น RTDB emulator checks ที่ยังไม่มี emulator ใน environment นี้
Python compile และ pip check ผ่าน; audit ของ 86 packages ใน test environment
พบ 0 known vulnerabilities หลังแก้ pip ของ venv จาก 25.0.1 เป็น 26.2.1
ผลนี้ไม่ใช่การรับรองว่าไม่มีช่องโหว่ และไม่ครอบคลุม deployed image/cloud configuration
การทดสอบ circuit ครอบคลุม single attempt/cold restart/cooldown/lease expiry,
probe ซ้อนกัน/late response, invalid quote และ recovery/dispatch/diagnostic wiring
concurrency ของ RTDB จริงและ secret-scan CI ยังต้องผ่านบน PR

Environment ไม่มี broker credentials หรือ cloud identity จึงยืนยัน account,
market-data entitlement, UAT endurance, deployed revision หรือเงินจริงไม่ได้
Webull developer domain และ GitHub API ถูก outbound policy บล็อก;
Git read ของ repository ผ่านได้ ไม่ได้พิสูจน์สิทธิ์ GitHub API
การบันทึก draft เพื่อเพิ่ม network/setup ไม่สำเร็จ จึงไม่ถือว่า config ถูกบันทึกหรือ apply
รายละเอียดผลทดสอบปัจจุบันและ artifact hashes อยู่ในรายงาน validation ที่ส่งพร้อม source

ตรวจ signature/request shape ของ snapshot เทียบ official SDK source ref
`abe5668ce4bc11dd1e8a1aad944d9dbb239bf12d` และ vendored SDK 3.0.1 ได้;
การตรวจเอกสาร Thailand ล่าสุดยังถูกบล็อก:

- https://developer.webull.co.th/apis/llms.txt
- https://developer.webull.co.th/apis/docs/sdk
- https://github.com/webull-inc/webull-openapi-python-sdk/blob/abe5668ce4bc11dd1e8a1aad944d9dbb239bf12d/webull/data/quotes/market_data.py

**สถานะ readiness: NO_GO สำหรับการรับรองเงินจริง** จนกว่า broker และ deployment
acceptance ข้างต้นผ่าน local tests เป็นหลักฐานของ source เท่านั้น
