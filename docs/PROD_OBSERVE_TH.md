# PROD observe และ preview: ใช้ PROD โดยไม่ใช้เงินจริง

มีสามขั้นตามลำดับ:
1. **observe** (หัวข้อแรก)
2. **preview** ([ทุกขั้นเหมือนเงินจริง ยกเว้น Place](#preview-ทุกขั้นเหมือนเงินจริง-ยกเว้น-place))
3. **paper** ([หุ้นและ fill ปลอม ให้ตาราง 17 คอลัมน์เดิน](#paper-หุ้นและ-fill-ปลอม-ให้ตาราง-17-คอลัมน์เดิน))

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
| ครั้งแรก และเมื่อ Check Token ของ Webull บอกว่า token ใช้ไม่ได้หรือเหลือไม่เกิน 3 วัน (สคริปต์ถามเองและแสดงวันหมดอายุที่ Webull ตอบ) | กดอนุมัติการ login OpenAPI ในแอป Webull ภายใน 5 นาที |

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

## Preview: ทุกขั้นเหมือนเงินจริง ยกเว้น Place

ใช้หลังจาก observe ผ่านแล้ว (token และ quote ใช้ได้) โหมดนี้ทำทุกอย่างที่ order จริงทำตามลำดับเดียวกัน:
1. decision สร้าง intent
2. ตรวจ token และ caps
3. อ่าน open orders และราคาสด แล้วตรวจอายุ quote ≤ 60 วินาที, อายุ decision ≤ 120 วินาที และราคาที่เลื่อน
4. **Preview จริง** ที่ broker (ได้ fee จริง) และตรวจ buying power
5. อ่านซ้ำหลัง Preview แล้วตรวจ caps อีกรอบ

จากนั้นหยุด: intent จบเป็น `UNSENT_ABORTED` ก่อน chain fence ไม่มีการ reserve quota, ไม่มี marker และ**ไม่เคยเรียก Place**

```bash
LEGO_MODE_OVERRIDE=preview bash deploy/cloudshell-prod-observe.sh
```

สคริปต์ถามเพิ่ม 3 ข้อ ก่อนเรียก cloud:

| คำถาม | ใช้ทำอะไร | ข้ามด้วย env |
|---|---|---|
| ราคาล่าสุดของ symbol | คิด cap จำนวนหุ้น | `LEGO_REFERENCE_PRICE` |
| บัญชีมีหุ้นประมาณ FIX_C แล้วหรือยัง | ยังไม่มี: ใช้ caps แบบ funding (≤ 125% ของ FIX_C) เพราะ order แรกของบัญชีว่างคือ FIX_C ทั้งก้อน; มีแล้ว: ใช้ caps แบบ canary | `LEGO_FUNDING_MODE_OVERRIDE=initial-funding` หรือ `prefunded` |
| Webull ยืนยันแล้วหรือยังว่า Preview ไม่สร้าง order | repo ยังไม่ได้ยืนยันเรื่องนี้กับหน้าเอกสารเต็ม ([runbook ข้อ 11](PROD_LIVE_RUNBOOK_TH.md)) | `LEGO_PREVIEW_CONFIRMED=yes` |

caps, window 5 session และ release authorization คำนวณให้เองด้วย `ops.py release-plan --recommended-limits`
release นี้ผูกกับ mode จึงใช้เปิด trade ไม่ได้ และ preview ไม่มี `LEGO_PROD_LIVE_ACK` ไม่ว่ากรณีใด

**ก่อนรัน**
- **มีเงินสด USD อย่างน้อยเท่าขนาด order:** บัญชีว่างจะได้ BUY ประมาณ FIX_C ทุก slot จึงต้องมีเงินสดประมาณ FIX_C + fee เช่น FIX_C 450 ต้องมีประมาณ 460
- **ใช้ FIX_C เล็ก:** ถ้า Preview บน PROD สร้าง order ขึ้นมาจริงจะได้เสียไม่เกินก้อนนี้
- **เฝ้าแอป Webull ตอน slot `READY` แรก:** ต้องไม่มี order เกิดขึ้น

**อ่านผล:** แถวที่ `preview_only=true` คือ Preview ที่ผ่านทุกด่านแล้ว ส่วน `preview_fee_pct` คือ fee จริงเป็น % ของมูลค่า

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed" AND jsonPayload.execution.preview_only=true' --project=lego-firebase --freshness=1d --limit=20 --format='table(timestamp,jsonPayload.execution[0].status,jsonPayload.execution[0].preview_fee_pct)'
```

intent ที่ถูกกันก่อนถึงจุดตัดจะจบเหมือนของจริงพร้อมเหตุผล เช่น `NOT_PLACED`, `SUPPRESSED_STATE_CHANGED` หรือ `quote_age_limit`
ดูเหตุผลได้ใน `execution[]` ของ log เดียวกัน ส่วน fee ที่วัดได้ใช้เป็น `--fee-pct` ตอนวางแผนเงินจริง

**tripwire:** ถ้าหลัง Preview เห็น open order หรือจำนวนหุ้นเปลี่ยน แปลว่ามีคนเทรดบัญชีนี้ หรือ Preview สร้าง order
- ระบบจะตั้ง operator halt ชื่อ `preview-tripwire` แล้วหยุดสร้าง intent ทันที
- ตรวจแอป Webull ก่อน แล้วปลดใน shell ที่ตั้ง `LEGO_ACTIVE=false` ด้วย `python ops.py clear-operator-halt --halt-id <id> --operator <ชื่อคุณ> --reason "<เหตุผล>"` (dry-run ก่อน แล้วจึงเติม `--apply`)

**สิ่งที่ preview พิสูจน์ไม่ได้ (ยังต้องมี canary):**
- ตัว Place เอง เช่นกรณี UAT 27–28 ต.ค. ที่ Place ได้ 200 แต่ order เป็น `FAILED`
- fill, fee จาก fill, การกู้ order และ cancel

**กลับไป observe:** รันคำสั่งเดิมโดยไม่ใส่ `LEGO_MODE_OVERRIDE` chain เดิมยังใช้ต่อได้ เพราะ mode ไม่ได้อยู่ใน chain key
**ต่ออายุหลังครบ 5 session:** รันคำสั่ง preview เดิมซ้ำ

## Paper: หุ้นและ fill ปลอม ให้ตาราง 17 คอลัมน์เดิน

ใช้หลังจาก preview ผ่านแล้ว paper ทำทุกอย่างเหมือน preview แต่ที่จุดที่ preview หยุด ระบบจะลงบัญชี fill ที่ market order น่าจะได้ให้หุ้นปลอม
slot ถัดไปจะตัดสินใจจากหุ้นปลอมนั้น ตาราง 17 คอลัมน์จึงเดินเหมือนเทรดจริง และ**ไม่เคยเรียก Place**

```bash
LEGO_MODE_OVERRIDE=paper bash deploy/cloudshell-prod-observe.sh
```

ถามเหมือน preview (FIX_C, ราคาล่าสุด และคำยืนยันเรื่อง Preview) แต่ไม่ถามว่าบัญชีมีหุ้นหรือยัง
เพราะ paper เริ่มจาก 0 หุ้นปลอมเสมอ จึงใช้ caps แบบ funding

| ขั้น | paper |
|---|---|
| quote, token, caps, window, DNA, decision, intent และด่านก่อน Place | จริง โดยคิดจากหุ้นปลอม |
| Preview ของ BUY และ SELL | จริง ถ้า Webull ปฏิเสธ SELL เพราะบัญชีจริงไม่มีหุ้น จะเก็บคำตอบไว้ใน `sell_preview` (ไม่นับเป็น error) แล้วไปต่อ |
| fill | ราคาซื้อขายล่าสุดของ quote ที่อ่านซ้ำหลัง Preview, จำนวนตาม intent, ไม่คิด spread หรือ slippage |
| ΔAₙ/Aₙ/Eₙ | `finalize_execution_fill` ตัวเดียวกับ fill จริง (fill แรกเป็น initial funding: dA/A/E = 0) |
| Place, fence, quota, reconcile, FIFO realized | ไม่มี |

**ข้อมูลอยู่ที่ไหน:** RTDB แยกชื่อ `lego-firebase-paper` และ chain แยก (`shannon_demon_lego_paper_v2`) ใน `lego-firebase-prod` จึงไม่มีข้อมูลปลอมปน
- สคริปต์สร้าง database นี้ วาง rules ปิดอ่านสาธารณะ และพิสูจน์ด้วย `curl` เหมือน PROD
- paper DB ยังมีข้อมูลบัญชีจริง (balance และผล Preview ใน trace) จึงต้องเป็นส่วนตัวเหมือน PROD
- ชื่อ RTDB ซ้ำกันทั่วโลกไม่ได้ ถ้า `lego-firebase-paper` มีคนใช้แล้ว ให้ตั้ง `LEGO_PROD_DB_INSTANCE=<ชื่อที่มีคำว่า paper>`
- `tools/verify_deployment.py` ตอบ FAIL ถ้า paper ชี้ database ที่ชื่อไม่มีคำว่า paper หรือถ้าโหมดอื่นชี้ database ของ paper

**ดูตาราง:** ใน dashboard ใส่ secret `FIREBASE_DB_URL_PAPER = "https://lego-firebase-paper.asia-southeast1.firebasedatabase.app"` แล้วเลือก PAPER ที่ sidebar

**ดู fill ปลอมใน log:**

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed" AND jsonPayload.execution.paper=true' --project=lego-firebase --freshness=1d --limit=20 --format='table(timestamp,jsonPayload.execution[0].status,jsonPayload.execution[0].paper_fill.price,jsonPayload.execution[0].paper_fill.holdings_after,jsonPayload.execution[0].preview_fee_pct)'
```

`preview_fee_pct` ของ order เล็ก (~25–33 USD) คือ fee จริงของ order ขนาดที่กลยุทธ์ส่งบ่อยที่สุด ใช้เป็น `--fee-pct` ตอนวางแผนเงินจริงได้

**tripwire:** หุ้นในบัญชีจริงต้องไม่ขยับระหว่างที่ paper ทำงาน
- ถ้าขยับ (ตอน decision หรือตอน dispatch) ระบบตั้ง operator halt ชื่อ `preview-tripwire` แล้วหยุดสร้าง intent
- halt นี้อยู่ใน **paper DB** จึงไม่ตามไปที่ `lego-firebase-prod` ต้องตรวจบัญชีในแอป Webull ก่อนกลับไป preview หรือ trade

**กลับไป preview:** `LEGO_MODE_OVERRIDE=preview bash deploy/cloudshell-prod-observe.sh` ซึ่งเขียน `lego-firebase-prod` และต่อ chain preview เดิม
**ลบข้อมูลปลอม:** ลบ instance `lego-firebase-paper` ใน Firebase console (Realtime Database) ไม่กระทบ PROD

**สิ่งที่ paper พิสูจน์ไม่ได้ (ยังต้องมี canary):**
- Place และ fill จริง (spread, slippage, partial fill, `FAILED`)
- fee ของ SELL ที่ Webull ปฏิเสธ Preview
- cancel, reconcile และ realized FIFO
