# PROD observe: ใช้ PROD โดยไม่ใช้เงินจริง

`lego-tick-prod` อ่านบัญชีจริงและราคาจริงที่ `api.webull.co.th` แล้วคำนวณแถวทุก slot เหมือน UAT
แต่ทำงานแบบ `observe/inactive`:
- ไม่สร้าง intent และไม่เรียก Preview, Place หรือ Cancel
- ไม่ต้องมีเงินในบัญชี

นี่คือขั้น "PROD observe ≥ 2 regular sessions" ของ [runbook เงินจริง](PROD_LIVE_RUNBOOK_TH.md) ส่วนเงินจริงยังเป็น NO-GO

## Deploy: Cloud Shell คำสั่งเดียว

```bash
cd lego && git pull
bash deploy/cloudshell-prod-observe.sh
```

ต้อง login `gcloud` และ `firebase login --no-localhost` ไว้แล้วเหมือนตอน deploy UAT สคริปต์จะถามเฉพาะสิ่งที่ยังไม่มี:

| เมื่อไร | ถามอะไร |
|---|---|
| ทุกครั้งที่ไม่ได้ตั้ง `LEGO_FIX_C_OVERRIDE` | FIX_C: observe ไม่ใช้เงินก้อนนี้ แต่เป็นส่วนหนึ่งของชื่อ chain |
| ครั้งแรก | app key และ app secret ของ PROD (ไม่แสดงบนจอ) และ account_id ถ้า app เห็นมากกว่าหนึ่งบัญชี |
| ครั้งแรก และเมื่อ token เหลือไม่เกิน 3 วัน (อายุประมาณ 15 วัน) | กดอนุมัติการ login OpenAPI ในแอป Webull ภายใน 5 นาที |

สิ่งที่สคริปต์ทำให้ (รันซ้ำได้ ขั้นที่เสร็จแล้วจะข้าม):
1. ตั้ง secrets `webull-*-prod` และเก็บ token ที่อนุมัติแล้วลง `webull-token-prod`
   function ของ PROD ออก token เองไม่ได้เพราะต้องมีคนกดอนุมัติ
2. สร้าง RTDB `lego-firebase-prod` แยกจาก DB ของ UAT
   - วาง `database.rules.prod.json` ซึ่งปิดการอ่านสาธารณะและมี index ที่ worker ใช้
   - พิสูจน์ด้วย `curl` ว่าอ่านโดยไม่ login ไม่ได้ ทั้งก่อนและหลัง deploy
3. รัน `deploy/cloudshell-all-in-one.sh` แบบ PROD + observe + inactive
   - ล้าง authorization, ack และ caps ที่อาจค้างจาก UAT
   - ผลที่ควรเห็นคือ `ORDER SUBMISSION = BLOCKED`
   - banner ท้ายสคริปต์เดิมยังเขียนว่า UAT ให้ดูบรรทัด `Environment  : PROD`

ค่าเริ่มต้นเปลี่ยนได้ด้วย env:
- `LEGO_SYMBOL_OVERRIDE` (UBER)
- `LEGO_DIFF_OVERRIDE` (25)
- `LEGO_DNA_BUNDLE_OVERRIDE` (`strategy.uat-continuous.json` เพราะ `strategy.example.json` หมดไปตั้งแต่ 2026-10-05)
- `LEGO_PROD_DB_INSTANCE` (`lego-firebase-prod`)

symbol, FIX_C, DIFF และ DNA เป็นตัวกำหนดชื่อ chain ทุกครั้งที่ deploy ซ้ำให้ใช้ค่าเดิม

## ตรวจให้ครบ ≥ 2 regular sessions

ทุกแถวต้องเป็น `PROD` / `observe` / `False`:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed"' --project=lego-firebase --freshness=1d --limit=20 --format='table(timestamp,jsonPayload.environment,jsonPayload.mode,jsonPayload.active,jsonPayload.business_status)'
```

คำสั่งนี้ต้องไม่มีผลลัพธ์เลย คือไม่มี Preview, Place หรือ Cancel ไปที่ broker:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_operation" AND (jsonPayload.operation="sdk_preview" OR jsonPayload.operation="sdk_place" OR jsonPayload.operation="sdk_cancel")' --project=lego-firebase --freshness=7d --limit=5 --format='value(timestamp,jsonPayload.operation)'
```

- `business_status` ต้องไม่เป็น `TOKEN_PREFLIGHT_BLOCKED` หรือ `AUTH_BACKOFF`
- quote ต้องสด: `python -m tools.trace_audit …` ต้องไม่มี `QUOTE_NOT_REALTIME` (ต้องมีสิทธิ์ market data แบบ real-time)
- ถ้า smoke ล้มด้วย `RECOVERY_ERROR` ให้รันสคริปต์ซ้ำเพื่อวาง rules และ index ของ PROD

## ควรรู้

- บัญชีที่ว่างจะได้ทุกแถวเป็น `READY_BUY ≈ FIX_C` เพราะระบบใช้ holdings จริง
  observe พิสูจน์การอ่าน token และ quote ได้ แต่ไม่ได้จำลองกำไร ถ้าจะจำลองด้วยเงินปลอมให้ใช้ UAT
- หยุดได้ด้วย `gcloud scheduler jobs pause lego-tick-prod --location=asia-southeast1 --project=lego-firebase` เพราะไม่มี order ให้ reconcile
- เงินจริงเป็น release แยกที่ต้องมี acknowledgement ดู [PROD_LIVE_RUNBOOK_TH.md](PROD_LIVE_RUNBOOK_TH.md) สคริปต์นี้เปิดเงินจริงไม่ได้
