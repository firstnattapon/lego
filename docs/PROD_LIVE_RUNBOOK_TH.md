# Runbook: เปิดเงินจริง (PROD live) แบบมี acknowledgement

**สถานะ: NO-GO จนกว่า gate ด้านล่างผ่านด้วยหลักฐานจริง** — เอกสารนี้อธิบายเส้นทางที่ source รองรับ ไม่ได้อนุมัติให้เปิด
ค่าเริ่มต้นของ PROD คือ observe/inactive; PR ที่เพิ่มเส้นทางนี้ไม่ได้เปิดเงินจริงให้ใคร
จำนวนเงิน (`LEGO_FIX_C`), caps, window และการตัดสินใจเปิด เป็นของเจ้าของบัญชีเท่านั้น — ไฟล์ตัวอย่างไม่ใส่ตัวเลขเงินแทนคุณ

## หลักการ

- runtime ส่ง order PROD ได้เมื่อครบทุกข้อ: `LEGO_MODE=trade`, `LEGO_ACTIVE=true`, release authorization ตรง,
  caps 4 ค่า (`LEGO_MAX_ORDER_QUANTITY`, `LEGO_MAX_ORDER_NOTIONAL_USD`, `LEGO_MAX_SESSION_ORDERS`, `LEGO_TRADING_WINDOW_END`)
  อยู่ในหน้าต่างอนุมัติ และ **`LEGO_PROD_LIVE_ACK` ตรงกับ release นี้** — ขาดข้อใดข้อหนึ่ง = ไม่ส่ง (fail closed)
- ack ผูกกับ symbol, quantity cap, notional cap, orders cap, เวลาสิ้นสุด window, โหมด funding และ release binding
  จึงนำ ack ของ release อื่นมาใช้ไม่ได้ และ release ที่ไม่ผ่านการตรวจแบบ static จะไม่มี ack ให้เลย
- ack เป็น **จุดตรวจทานของมนุษย์ ไม่ใช่ลายเซ็นเข้ารหัส**: ผู้ที่แก้ env ของ function ได้ย่อมใส่ ack เองได้
  ป้องกันการ deploy ผิดพลาดและ rollout ที่ไม่ได้อ่านตัวเลข ไม่ได้ป้องกันผู้ที่มีสิทธิ์ IAM แก้ deployment
- ไม่มีอะไรต่ออายุเอง: window, DNA และ token หมดแล้วระบบหยุดส่ง order (และเตือนล่วงหน้า 48 ชม.) จนกว่าคนจะออก release ใหม่

## 0. ก่อนเริ่ม (ทุกข้อต้องมีหลักฐาน)

| ข้อ | เกณฑ์ |
|---|---|
| UAT order path | BUY และ SELL positive fill ≥ 2 regular sessions, `finalized_seq>0`, holdings ตรง broker, ไม่มี order ซ้ำ (G2) |
| UAT failure drills | restart กลาง order, token หาย/หมด → หยุดปลอดภัยและมี alert (G3) |
| Alert | apply `tools/monitoring_config.py`, ตั้ง `ALERT_WEBHOOK_SECRET_OVERRIDE`, ซ้อม `python ops.py alert-drill --secret projects/P/secrets/N` (dry-run) แล้ว `--send` ให้ถึงผู้รับจริง (G4; ผล `SENT` ไม่ใช่หลักฐานว่าคนเห็น — `delivery_verified` เป็น false เสมอ ต้องมีคนยืนยัน) |
| PROD observe | ≥ 2 regular sessions: authenticated reads, quote สด, ไม่มี new-order mutation; ยืนยันสิทธิ์ market data real-time |
| Token | operator-issued, `NORMAL`, เหลือ > 3 วัน (`LEGO_TOKEN_REFRESH_MARGIN_DAYS`: runtime ไม่ส่ง order ใหม่ถ้าเหลือน้อยกว่านั้น แต่ยังอ่านและ reconcile ได้จนหมดอายุ), หมุนก่อนวันที่ 12 ของอายุ |
| แยกจาก UAT | account/secrets `*-prod`, service `lego-tick-prod`, **RTDB แยก**: script บังคับให้ระบุ `DATABASE_URL_OVERRIDE` สำหรับ PROD ทุกโหมด (ไม่ auto-discover แล้ว) และ rules ของ DB นั้นต้องปิดอ่านสาธารณะ พิสูจน์ด้วย `curl` ตามหัวข้อ 2b |
| ต้นทุน | วัด fee จริงด้วย Preview Order หลายขนาด (`estimated_transaction_fee ÷ estimated_cost`) ด้วย `AUTO_SUBMIT=false python tools/measure_fee.py --allow-prod --notionals 25,50,100,250,1000` (Preview อย่างเดียว ไม่ import Place/Cancel; ได้ `suggested_fee_pct` = ค่ามากสุดปัดขึ้น, `payload_shape`, อายุ quote) แล้วส่งให้ `release-plan --fee-pct` (ไม่ส่ง = WARN `fee_unmeasured`). fee เป็น**ต้นทุนล้วน**: กลยุทธ์ถือ ≈FIX_C ดอลลาร์คงที่ ไม่มี edge ในตัวเมื่อราคาเป็น random walk — ตารางและข้อสรุปใน [AUDIT_20261010](AUDIT_20261010_TH.md) หัวข้อ "ต้นทุน" (fee 0.15% ที่ DIFF 25 ได้ผลสุทธิ ≈ 0 ในโลกที่ดีที่สุด); การตัดสินใจเปิดเป็นของเจ้าของบัญชี ไม่ใช่ gate ของโค้ด |

## 1. เลือกวิธี funding (ตัดสินใจก่อนวางแผน)

order แรกของบัญชีที่ไม่มี position ≈ `LEGO_FIX_C` ทั้งก้อน เป็น order ที่ใหญ่ที่สุดที่ระบบทำได้

| วิธี | ทำอย่างไร | ack |
|---|---|---|
| **A. pre-fund (แนะนำ)** | ซื้อ position ประมาณ FIX_C ด้วยมือก่อน แล้วออก release steady เพียงรอบเดียวด้วย caps แน่น (notional ≤ 25% ของ principal) | `…-prefunded-…` |
| **B. initial-funding** | สอง release: (1) funding 12,500 / qty 200 / orders 2 สำหรับ FIX_C=10000 (125% ของ principal) → รอ fill ยืนยันและ holdings ตรง (2) deploy release steady ทันที | `…-initial-funding-…` |

B คือ order ก้อนเดียวที่ใหญ่ที่สุด จึงใช้ caps หลวมโดยออกแบบ — ต้องเป็น release สั้น ๆ และห้ามปล่อยค้าง
`release-plan` บล็อก PROD ที่ notional เกิน 25% ของ principal ถ้าไม่ใช่ `--initial-funding` และบล็อก funding ที่ต่ำกว่า 101% (order t0 เลื่อนราคาได้ ≤100 bps)
รายละเอียดการลงบัญชีของ funding BUY: `docs/FUNDING_BASELINE_20260916.md`

## 2. วางแผน release

```bash
cp deploy/prod-canary.env.example private-prod.env   # กรอก LEGO_SYMBOL, LEGO_FIX_C, LEGO_DNA_BUNDLE ด้วยตัวเอง
gcloud secrets versions access latest --secret=webull-account-id-prod --project=lego-firebase | \
  python ops.py release-plan --env-file private-prod.env --account-id-stdin \
      --window-sessions 5 --reference-price <ราคาล่าสุด> --recommended-limits --fee-pct <fee% ที่วัดด้วย Preview> \
      --enforce > private-plan.json
# release funding (วิธี B รอบแรก): เพิ่ม --initial-funding
```

- `--window-sessions N` สิ้นสุดที่ปิดตลาดของ session ที่ N (ต้องครอบ ≥ 2 session สมบูรณ์); canary แนะนำช่วงสั้น เช่น 5 session
- `--recommended-limits` ให้ notional 10% ของ principal, orders 10 ต่อ session, quantity = 1.3 × notional ÷ ราคา ปัดขึ้นเป็นเลขกลม — เป็นจุดเริ่ม
  ตัดสินใจเองได้ แต่ notional > 25% ของ principal เป็น BLOCK; DNA ต้องจบหลัง window (เตือนถ้าเหลือ < 2 session)
- `--enforce` exit 1 เมื่อมี BLOCK; ผลลัพธ์เป็น private: มี release authorization และ ack; ไม่พิมพ์ account id
- PROD เริ่มด้วย whole shares (`LEGO_ALLOW_FRACTIONAL=false`) — `true` บน PROD เป็น WARN ให้ทบทวนอย่างชัดเจน
- candidate hash คำนวณจาก checkout ที่ commit แล้วและสะอาด (script ปฏิเสธ working tree ที่ไม่สะอาด)

## 2b. ฐานข้อมูล PROD (ต้องทำก่อน deploy)

RTDB เก็บจำนวนถือครอง ทุก order และ P&L. `database.rules.json` (ของ UAT) เปิด `.read` สาธารณะที่ 5 node (`rows`, `state`, `order_audit`,
`order_audit_archive`, `warnings`) เพื่อให้ dashboard UAT อ่านได้ — **ห้ามใช้ rules นี้กับ PROD** และ script ไม่เลือก DB ให้ PROD อีกแล้ว
(เดิม auto-discover `-default-rtdb` ซึ่งเป็น DB ที่ UAT ใช้อยู่; ดู finding F6 ใน [AUDIT_20261010](AUDIT_20261010_TH.md))

1. สร้าง RTDB instance แยกสำหรับ PROD (ไม่ใช่ `*-default-rtdb` ที่ UAT ใช้) แล้ว `export DATABASE_URL_OVERRIDE=https://<instance-prod>.firebasedatabase.app`
   — ไม่ตั้งค่านี้ script จะ `exit 1` ก่อนเรียก cloud ใด ๆ ทั้งโหมด observe และ trade
2. ใช้ [`database.rules.prod.json`](../database.rules.prod.json) กับ instance นั้น: เหมือน `database.rules.json` ทุกประการ ยกเว้น `.read:false` ทุก node
   (`test_audit_20261010.py` กัน drift: สองไฟล์ต้องต่างกันเฉพาะ `.read`). Cloud Run เขียนผ่าน Admin SDK ซึ่งไม่ผ่าน rules จึงทำงานเหมือนเดิม
   แต่ dashboard สาธารณะแบบ UAT จะอ่านไม่ได้ (ตั้งใจ — ถ้าต้องการ dashboard PROD ให้เพิ่มการอ่านแบบ authenticated เองแล้วตรวจข้อ 3 ซ้ำ)
   - ขั้น 8 ของ script รัน `firebase deploy --only database` ซึ่งลง `database.rules.json` ให้**เฉพาะ default instance** ของ project (= DB ของ UAT):
     มันไม่ลง rules PROD ให้ instance แยก และห้ามเอา rules PROD ไปทับ default instance. ใช้วิธี multi-instance ของ Firebase CLI
     (deploy target ต่อ instance) หรือวาง rules ผ่าน console — ผู้เขียนทดสอบพฤติกรรม CLI นี้แบบออฟไลน์ไม่ได้ จึง**ไม่ผูกเข้า script** และใช้ข้อ 3 เป็น gate แทน
3. พิสูจน์ด้วยตัวเองก่อนเปิด PROD (ต้องได้ `Permission denied`; ได้ข้อมูลกลับมา = rules ผิด หยุด):

   ```bash
   curl -s "$DATABASE_URL_OVERRIDE/webull_lego_rows.json?shallow=true"
   curl -s "$DATABASE_URL_OVERRIDE/webull_lego_state.json?shallow=true"
   ```

4. ทำซ้ำข้อ 3 หลังทุก deploy (หัวข้อ 4): ขั้น 8 ลง `database.rules.json` (อ่านสาธารณะ) ทุกครั้ง ถ้า `firebase.json` ของคุณถูกแก้ให้ชี้ instance PROD
   rules สาธารณะจะไปทับ rules PROD โดยไม่มีอะไรเตือน

## 3. Deploy

ทำ**นอกเวลา regular session เท่านั้น** (09:30–16:00 New York) — script ปฏิเสธระหว่าง session เพราะ smoke tick ท้าย script เป็น tick ปกติ

```bash
export WEBULL_ENV_OVERRIDE=PROD LEGO_MODE_OVERRIDE=trade LEGO_ACTIVE_OVERRIDE=true
export WEBULL_TOKEN_SECRET_OVERRIDE=projects/lego-firebase/secrets/<token-secret>
export ALERT_WEBHOOK_SECRET_OVERRIDE=<secret ที่เก็บ URL ปลายทาง>
export DATABASE_URL_OVERRIDE=https://<instance-prod>.firebasedatabase.app   # บังคับสำหรับ PROD ทุกโหมด (หัวข้อ 2b); ขาด = script exit 1
export LEGO_FEE_PCT_OVERRIDE=<fee% ที่วัดด้วย Preview>                       # ไม่บังคับ; ส่งต่อให้ release-plan --fee-pct (ขาด = WARN fee_unmeasured)
# ห้ามตั้ง WEBULL_API_DEBUG: บน PROD runtime ปฏิเสธด้วย CONFIG_ERROR (SDK จะ log header/body ของทุก request)
# ค่าจาก private-plan.json -> deploy_env: EXPECTED_CANDIDATE_HASH, LEGO_RELEASE_AUTHORIZATION_OVERRIDE,
# LEGO_TRADING_WINDOW_END_OVERRIDE, LEGO_MAX_*_OVERRIDE, LEGO_SYMBOL_OVERRIDE, LEGO_FIX_C_OVERRIDE, LEGO_DNA_BUNDLE_OVERRIDE ฯลฯ
export LEGO_FUNDING_MODE_OVERRIDE=initial-funding   # เฉพาะ release funding ของวิธี B (release-plan --initial-funding พิมพ์ไว้ใน deploy_env)
bash deploy/cloudshell-all-in-one.sh        # รอบแรก: พิมพ์ ack ที่คาดหวังแล้ว exit 3 ก่อน deploy function
```

1. รอบแรกทำขั้นเตรียม (API, service account, secret, IAM — idempotent) ตรวจ release ทั้งชุด แล้ว**หยุดที่ exit 3** พร้อมพิมพ์
   `LEGO_PROD_LIVE_ACK=LIVE-PROD-<SYMBOL>-q<qty>-n<notional>-o<orders>-<window end>-<prefunded|initial-funding>-<binding[:16]>`
2. อ่านทุกค่าให้ตรงกับที่ตั้งใจ (symbol, caps, window, โหมด funding) แล้วรันคำสั่งเดิมซ้ำพร้อม `LEGO_PROD_LIVE_ACK=<ค่าที่พิมพ์>`
3. ack ผิดหรือเป็นของ release อื่น → fail; release ที่ไม่ผ่านการตรวจ → ไม่มี ack ให้ ต้องแก้ตาม `[BLOCK]` ก่อน
4. script ตรวจ release ซ้ำก่อน deploy โดยถือเป็น release steady ตามค่าเริ่มต้น (`prefunded`): caps เกิน 25% ของ principal จึงถูก BLOCK
   — ตั้งใจให้เป็นเช่นนั้นเพื่อไม่ให้ caps หลวมหลุดไปโดยบังเอิญ; release funding ต้องตั้ง `LEGO_FUNDING_MODE_OVERRIDE=initial-funding` เองอย่างชัดเจน
   (ค่านี้ตรวจ caps แบบ funding: notional ≥ 101% ของ principal และไม่เกิน 125% — caps steady จะถูก BLOCK ถ้าขอโหมดนี้)
5. `deploy/deploy.ps1` ไม่รองรับ PROD live (throw) ใช้ script นี้เท่านั้น

## 4. หลัง deploy

```bash
python ops.py status      # ไม่มี inflight, ไม่มี operator halt, ไม่มี unresolved fence
python tools/verify_deployment.py --service private-service.json --scheduler private-scheduler.json \
    --function private-function.json --candidate HASH --revision REVISION --image REGISTRY/IMAGE@sha256:DIGEST
```

- ทำซ้ำ `curl` ของหัวข้อ 2b ข้อ 3 กับ `DATABASE_URL_OVERRIDE` — ต้องยังได้ `Permission denied`
- `release-plan` แสดง WARN `alert_unset` เมื่อไม่มี `ALERT_WEBHOOK_SECRET_OVERRIDE`; ถ้าใช้ Monitoring policy (`tools/monitoring_config.py`) แทน webhook
  ให้ยืนยันว่า policy ส่งถึงคนจริงด้วย drill (G4) — คำเตือนนี้ไม่รู้เรื่อง policy จึงไม่หายเอง
- `verify_deployment` ตรวจ `production_mode`: observe/inactive หรือ trade/active ที่มี ack, authorization และ caps ครบ — ไม่ได้ตรวจว่าค่าถูกต้องต่อบัญชี
- `business_status=RELEASE_UNAUTHORIZED` (severity ERROR, `operational_health.orders_blocked_by_release=true`) = deployment เป็น trade/active แต่ไม่ส่ง order เพราะ release authorization หรือ `LEGO_PROD_LIVE_ACK` ไม่ตรงกับ release (มักเกิดจากการแก้ env ของ function ภายหลัง) — `python ops.py check` แสดง `release_authorized`, `prod_live_gate_open`, `new_orders_authorized`; แก้ด้วยการ deploy ใหม่ผ่าน script ไม่ใช่แก้ env ทีละค่า
- เฝ้า tick แรกของ session ถัดไปด้วยตัวเอง: `business_status`, `release_expiring`, order แรกและ fill ตรงกับ caps ที่อนุมัติ
- วิธี B: ทันทีที่ funding fill ยืนยันและ holdings ตรง broker ให้ออก release steady (caps แน่น, `--initial-funding` ออก) — อย่าปล่อย release funding ไว้

## 5. Canary

symbol เดียว, whole shares, caps ต่ำ, window สั้น, เฝ้าหลาย session แรกด้วยตัวเอง กำหนดเงื่อนไขหยุดล่วงหน้า:
`needs_manual_check`, `order_contract_anomaly`, `OPEN_ORDER_BLOCKED` ที่ไม่หาย, holdings ใน state ต่างจาก broker, broker reject circuit เปิด
(บน PROD ไม่มีการลองเอง — halt ค้างจนกว่าคนจะอ่าน order ที่ถูกปฏิเสธแล้วเคลียร์ ดูข้อ 13)
ครบ controlled scope แล้วหยุดเพิ่ม exposure จนกว่าผลรับรองและ authorization ใหม่ครอบคลุมการทำต่อ

## 6. Kill switch (หยุด order ใหม่ — ไม่ลบอะไร)

| ต้องการ | คำสั่ง | ผล |
|---|---|---|
| หยุดทันที | `python ops.py halt-orders --operator <ชื่อจริง> --reason <เหตุผล>` แล้ว `--apply` | operator halt: ไม่รับ intent ใหม่; recovery/reconcile ของ order เดิมยังทำต่อ; ปลดด้วย `clear-operator-halt` ต้องมีผู้ตรวจคนที่สองและทำหลัง closure |
| ถอยเป็น observe | deploy PROD อีกครั้งแบบ observe/inactive (`LEGO_MODE_OVERRIDE=observe LEGO_ACTIVE_OVERRIDE=false`) | revision ใหม่ไม่ส่ง order ใหม่; order ค้างยังถูก reconcile และใช้ cancel policy ที่บันทึกไว้กับ intent เดิม (ไม่ใช่ยกเลิกทุก order ทันที) |
| window หมด | ไม่ต้องทำอะไร | blocked โดยอัตโนมัติ — และเป็นสถานะที่ตั้งใจ |

ห้ามหยุด Cloud Scheduler เป็นวิธีแรก (หยุด recovery ด้วย), ห้ามลบ outbox/fence/state, ห้ามเปลี่ยน status ใน RTDB เพื่อเปิด gate,
ห้าม reset origin/p0/ledger หรือเปลี่ยน account เพื่อข้าม fence

## 7. Token rotation (ทุก ≤ 15 วัน, โดยคน)

token ของ PROD ออกผ่านขั้นตอน Webull ที่รองรับ (ต้องยืนยันตัวตนในแอป) ไม่ต่ออัตโนมัติ — เตือนเมื่อเหลือ ≤ 7 วัน และ runtime หยุดส่ง order ใหม่เมื่อเหลือ ≤ 3 วัน (ไม่ใช่ 24 ชม. ตามที่เอกสารเดิมเขียน — `token_health().ready`); การอ่านและ reconcile order ที่เปิดอยู่ยังทำได้จนกว่า token จะหมดอายุ ([audit 7 ตุลาคม](AUDIT_20261007_TH.md))

```bash
python ops.py bootstrap-auth --token-file PRIVATE_TOKEN --secret projects/lego-firebase/secrets/<token-secret>          # dry-run
python ops.py bootstrap-auth --token-file PRIVATE_TOKEN --secret projects/lego-firebase/secrets/<token-secret> --apply  # เพิ่ม secret version
```

จากนั้นชี้ deployment ไปที่ version ใหม่ (`WEBULL_TOKEN_SECRET_OVERRIDE`), cold restart และพิสูจน์ว่า authenticated read ผ่านก่อนถึง session ถัดไป
ไม่ใส่ token ใน command line, log หรือ PR

## 8. ต่ออายุ window / DNA

- **window**: ออก release ใหม่ด้วย `release-plan` ก่อนหมด ≥ 48 ชม. (alert `RELEASE_EXPIRING` เตือนที่ 48 ชม.) และต้องมี ack ใหม่ทุกครั้ง
- **DNA**: `python -m tools.make_dna_bundle --dna-code bypass:N --origin <origin เดิม> --interval 900 --output <ไฟล์ใหม่>` แล้วอ้างผ่าน `LEGO_DNA_BUNDLE`
  dna_code ใหม่ = `config_hash`/chain ใหม่ ต้องทำตอนไม่มี order ค้าง และ chain ใหม่เริ่มจาก holdings จริง (ไม่มี funding baseline) — วางแผนก่อน DNA เดิมจบ
  (`release-plan` บล็อก release ที่ DNA จบก่อน window; `operational_health` เตือนเมื่อเหลือน้อยกว่าสอง session)
- **token**: ข้อ 7

## 9. Rollback

ปิด new orders (ข้อ 6) แล้วคง worker ที่เข้าใจ schema v4 ไว้เพื่อ reconcile ต่อ — ไม่ย้อนกลับไป binary ที่ไม่รู้จักสถานะใหม่,
ไม่ลบ outbox/fence, ไม่ refund quota (`LEGO_MAX_SESSION_ORDERS` นับก่อน Place และไม่คืน), ไม่ clear operator halt อัตโนมัติ
หลัง order ค้างทั้งหมด terminal และ ledger ปิดครบ จึงออก release ใหม่
ถ้า chain ถูกผูกกับ quantity contract ที่แคบลงแล้ว (เช่น 5→2 ตำแหน่ง) revision ที่ใช้ contract กว้างกว่าจะ commit ไม่ได้ (`CONFIG_ERROR`, ไม่มี order, DNA clock หยุด) — roll forward ไม่ roll back (`docs/UAT_20261005_INCIDENT_TH.md`)

## 10. ตาราง caps สำหรับ FIX_C=10000 (ตัวอย่าง ไม่ใช่คำแนะนำให้ใช้เงินจริงขนาดนี้)

| release | notional | quantity (ที่ราคา ~67) | orders/session | ack |
|---|---|---|---|---|
| steady canary | 1,000 (10%) | 20 | 10 | prefunded |
| steady เพดานสูงสุดที่ gate ยอม | 2,500 (25%) | — | ≤ 26 | prefunded |
| funding รอบเดียว | 12,500 (125%) | 200 | 2 | initial-funding |

ตรวจด้วย `release-plan` เสมอ: ค่าที่ไม่ผ่านจะไม่ได้ ack — อย่าแก้เลขเพื่อให้ผ่านโดยไม่เข้าใจเหตุผล (ดู `docs/AUDIT_20261005_TH.md`)

## 11. เส้นทางที่ UAT ไม่เคยทดสอบ (ครั้งแรกจะเกิดบน PROD)

UAT ผ่านครบหลาย session แล้ว แต่สิ่งต่อไปนี้ยังไม่เคยรันกับ broker จริง — ต้องเห็นครั้งแรกในขั้น observe/canary ไม่ใช่ตอนมีเงินเสี่ยงเต็มจำนวน

| เส้นทาง | ทำไม UAT ไม่ครอบ | ปิดด้วย |
|---|---|---|
| Token auth (hydrate จาก Secret Manager → `token.txt` → verify → refresh margin) | UAT ใช้ HMAC (`_check_token_enable` เป็น False); เส้นทางนี้มีแต่ unit test | PROD observe ≥ 2 session: tick ไม่เป็น `TOKEN_PREFLIGHT_BLOCKED` / `AUTH_BACKOFF`, `operational_health.token_warning` เป็น false, และหมุน token ตามหัวข้อ 7 สำเร็จอย่างน้อย 1 ครั้ง |
| Preview บน PROD และ payload MARKET/DAY/QTY/CORE | `lego_broker_smoke` ปฏิเสธ Preview บน PROD โดยตั้งใจ | `tools/measure_fee.py --allow-prod` (ข้อสมมติ "Preview ไม่สร้าง order" มาจากดัชนีเอกสาร Webull เท่านั้น — เปิดหน้าเต็มไม่ได้; ยืนยันกับ Webull ก่อนรันครั้งแรก) |
| Fee parser เข้ม (`lego_orders._actual_fee`) | fee UAT มีรูปแบบเดียว | canary order แรก: ตรวจ `AWAITING_BROKER_FEE` / `FEE_OVERDUE` |
| USD buying power ของบัญชี PROD (`validate_preview_funding`) | บัญชี UAT มี buying power เสมอ | `measure_fee --side BUY` ที่ notional เท่า order จริง + cash USD พอ |
| Whole-share (`LEGO_ALLOW_FRACTIONAL=false`) | UAT รันเศษหุ้นอย่างเดียว | UAT control run ก่อนด้วย `LEGO_ALLOW_FRACTIONAL=false` อย่างน้อย 1 session |
| อายุ quote ≤ 60 วินาที ตอน dispatch | quote UAT หน่วง ~15 นาทีโดยดีไซน์ (median 903 s ใน audit 15 ต.ค.) | `tools.trace_audit` จาก PROD observe: ต้องไม่มี `QUOTE_NOT_REALTIME` (P1) / `QUOTE_OFTEN_STALE` (P2) |
| cash account / settlement | ไม่มีใน UAT | ถาม Webull ก่อน |

## 12. เมื่อ UAT หยุดเพราะ order ค้าง (`MANUAL_RECONCILIATION_REQUIRED` / `OPERATOR_HALT`)

ห้ามแก้ RTDB ด้วยมือ, ห้ามลบ halt/fence, ห้าม Place ใหม่ด้วย ID ใหม่ — fence ผูกกับ order เดิมจนกว่า order นั้นปิดบัญชีครบ

1. **หลักฐาน (read-only)**: `python ops.py status` (halt, fence, intent ที่ `needs_manual_check`), `python ops.py inspect-open-orders`, อ่าน Order Detail ของ order นั้นที่ broker;
   ดู `cancel_last_error_code` ของ intent: `CANCEL_REFUSED_NOT_OPERABLE` (417 ชัดเจน) ต่างจาก `CANCEL_OUTCOME_UNKNOWN` / `TICK_DEFERRED` (cancel ไม่ทราบผล) ซึ่งไม่มี `cancel_refused_at`
2. **เลือกทางตามสถานะที่ broker** (เครื่องมือกู้ต้องไม่มี worker ถือ fence: หยุด Scheduler ชั่วคราวและรอ lease หมด — เฉพาะขั้นนี้ แล้วเปิดกลับ):
   - order **terminal** (FILLED / CANCELLED / EXPIRED): `python -m tools.resume_order_reconciliation --chain <chain> --run <run id เต็ม> --operator <ชื่อ>` (dry-run พิมพ์ `RESUME <sha>`) เทียบกับหลักฐาน แล้วรันซ้ำด้วย `--confirm "RESUME <sha>"`
   - order **non-terminal และ cancel ถูก 417**: เติม `--expiry-proof` (UAT: PENDING + filled 0 + อายุ ≥ 30 นาที + holdings ไม่เปลี่ยน; PROD: session จบ + 1 ชม. + ไม่อยู่ใน open orders) ถ้า blocker ไม่ครบ **ห้ามฝืน**
   - order **non-terminal และ cancel ไม่ทราบผล** (ไม่มี `cancel_refused_at`): ไม่มีเครื่องมือปล่อย (`--expiry-proof` จะบอก `cancel_not_refused`) — ส่ง order id + request id ให้ Webull แล้วรอให้ broker retire order (zombie เดิมหายภายในไม่กี่วัน) จึงกลับไปทางแรก
3. **รอ 1–2 tick** ให้ worker อ่าน broker ซ้ำและปล่อย fence; ตรวจ `python ops.py status` (fence ว่าง, `needs_manual_check` false)
4. **ปลด halt เป็นขั้นสุดท้าย** ใน shell ที่ตั้ง `LEGO_ACTIVE=false`: `python ops.py clear-operator-halt --halt-id <halt id เต็ม> --operator <ผู้ตรวจคนที่สอง> --reason "<เหตุผล>"` (dry-run) แล้ว `--apply`;
   ปฏิเสธเองเมื่อ fence ยังมี order ค้าง, audit ค้าง, worker ยัง active หรือผู้ปลดเป็นคนเดียวกับผู้ตั้ง
5. ส่ง evidence pack ให้ Webull ทุกครั้งที่เกิด (รูปแบบตาราง ดู `docs/AUDIT_20261015_TH.md` หัวข้อ "หลักฐานสำหรับ Webull")

บน PROD ไม่มี cancel อัตโนมัติ (นโยบาย `hold`) และ `cancel_expire` ถูกปฏิเสธโดย config — order ค้างบน PROD กู้ได้ด้วยข้อ 2 ทางแรกหรือทางที่สองเท่านั้น และต้องมีคน

## 13. เมื่อหยุดเพราะ broker reject circuit (`BROKER_REJECT_HALT`)

กลไก: broker ตอบ `FAILED` / `REJECTED` ติดกัน 3 order ⇒ circuit ตัด (`halted`, `halt_id` = run ใบที่ 3, `halted_at`) และไม่ส่ง order ใหม่ (`PASS_BROKER_REJECT_HALT`) — fence, ledger และ holdings ไม่ขยับ.
เหตุการณ์จริง 16 ต.ค. (SELL UBER 3 ใบ `FAILED` ไม่มีเหตุผล): `docs/AUDIT_20261016_TH.md`

| environment | เกิดอะไรต่อ |
|---|---|
| **UAT** | หลัง cooldown 900 s นับจาก `halted_at` (×2 ต่อ probe ที่พลาด: 900 → 1,800 → 3,600 → 7,200 → 14,400 s เพดาน) ที่ slot `READY_BUY`/`READY_SELL` แรก ระบบปล่อย order ปกติ **ใบเดียว** เป็น probe: `FAILED`/`REJECTED` ⇒ halt กลับทันที (`halt_id` ใหม่, `probe_streak`+1); fill ⇒ circuit กลับ `READY`. ต้องไม่มี order ค้าง, operator halt หรือ lease — ไม่ครบ halt คงอยู่. warning `BROKER_REJECT_AUTO_PROBE` บันทึกทุกครั้งที่เปิด |
| **PROD** | **ไม่มีการลองเอง** (ไม่มีตัวแปร environment ใดเปิดได้) — halt ค้างจนกว่าคนจะอ่าน order ที่ถูกปฏิเสธแล้วเคลียร์. นี่คือเงื่อนไขหยุด canary (ข้อ 5) ไม่ใช่สิ่งที่รอให้หายเอง |

**Pager**: halt ที่ค้างเป็น `ERROR` เฉพาะ 180 วินาทีแรกของทุกชั่วโมงนับจาก `halted_at` (ที่เหลือเป็น `INFO`) — page เมื่อ halt เกิด และเตือนซ้ำทุกชั่วโมงเหมือน `MANUAL_RECONCILIATION_REQUIRED`;
halt ใหม่หลัง probe พลาดเริ่มนับใหม่ (page ทุกครั้งที่ halt เกิดใหม่); `halted_at` อ่านไม่ได้/ไม่มี timezone/อยู่ในอนาคต ⇒ `ERROR` ทุก tick (fail loud).
incident ของ health-policy ปิดเองหลัง 30 นาทีที่ไม่มี `ERROR` (`autoClose` 1800 s) แล้วเปิดใหม่ในชั่วโมงถัดไป — ถูกต้องตามออกแบบ ไม่ใช่ alert หลุด.
ก่อน 2026-10-20 เป็น `ERROR` ทุก tick (391 จาก 438 tick; 48 tick หลังปิดตลาด) — health-policy จะ page ทุก ~5 นาทีทั้งคืน (`docs/AUDIT_20261020_TH.md` S2).
**จังหวะ reset บน UAT**: build UAT เปิด `HALF_OPEN` เองที่ slot `READY` แรกหลัง cooldown ครบ (ปกติ slot 0 ของ session, 13:30:05Z ซึ่งใช้ราคาปิดเมื่อวานจึงถูก `quote_age_limit` กัน — circuit ค้าง "armed" ไม่ใช่นาฬิกา: order ถัดไปที่ quote สดคือ probe);
คำสั่ง reset ใช้ได้เฉพาะตอนที่ยัง `halted=true` จึงต้องทำ**ก่อน**เวลานั้น. **redeploy ไม่เคลียร์ halt** — fence ผูกกับ account + symbol ไม่ผูกกับ candidate.

1. **หลักฐาน (read-only)**: `python -m tools.trace_audit --logs LOGS.json --rtdb EXPORT.json --yaml YAML.txt` — `BROKER_REJECT_HALT_ACTIVE` บอก halt id, ตั้งแต่เมื่อไร, tick/slot ที่เสีย, จำนวน probe และ failed-probe streak;
   `python ops.py status`; อ่าน Order Detail ของ order ที่ล้มทุกใบ เก็บ broker order id + request id ของ Preview/Place/Order Detail ส่งให้ Webull (รูปแบบตาราง ดู "หลักฐานสำหรับ Webull" ใน `docs/AUDIT_20261016_TH.md`)
2. ตรวจว่า fence ว่าง (ไม่มี order ค้าง, ไม่มี lease) และ holdings/open orders ที่ broker ตรงกับ state; ถ้ามี order ค้างให้ทำข้อ 12 ก่อน
3. **เคลียร์** (ไม่ต้องหยุดหรือ redeploy service; คำสั่งไม่เปิดการเทรดให้เอง) ใน shell ที่ตั้ง env เดียวกับ service แต่ `LEGO_ACTIVE=false`:
   `LEGO_ACTIVE=false python ops.py reset-reject-halt --halt-id <halt id เต็ม> --reason "<เหตุผล>"` (dry-run) แล้วเติม `--apply`.
   ปฏิเสธเองเมื่อ halt เปลี่ยน/หาย, fence ยังมี order ค้าง หรือ worker ถือ lease; circuit ที่เป็น `HALF_OPEN` ตอบว่า "nothing to reset" (ถูกต้อง: order ถัดไปคือ probe)
4. หลัง reset ตัวนับกลับเป็น 0 — FAILED ติดกันอีก 3 ใบจะ halt ใหม่. ถ้า `failed-probe streak` ≥ 2 หรือ halt ซ้ำภายในวันเดียว อย่า reset ซ้ำ ๆ: หยุดรอ Webull

ห้ามแก้ `broker_reject_circuit` ใน RTDB ด้วยมือ และห้ามใช้ reset เพื่อข้าม order ที่ยังค้างที่ broker (ใช้ข้อ 12)

## 14. UAT whole-share control (`LEGO_ALLOW_FRACTIONAL=false`) และเกณฑ์ canary จาก audit 20 ต.ค.

ทำไม: order `FAILED` ไม่มีเหตุผล 10 ใบติด (16 และ 19 ต.ค.) แยกสาเหตุด้วยโค้ดไม่ได้ และ whole-share คือรูปแบบ order ที่ PROD เริ่มต้น (`prod-canary.env.example`) ที่ UAT ไม่เคยรัน (ข้อ 11).
สมมติฐาน "เศษหุ้นเท่านั้น" อ่อน (UAT เคย FILL เศษหุ้น) — control นี้แยกตัวแปรเดียว: เศษหุ้น vs เต็มหุ้น.

1. **วางแผน release ด้วยค่าเดียวกับที่จะ deploy** — authorization ผูกกับ `allow_fractional`: `cp deploy/uat-continuous.env.example private-uat-wholeshare.env`, ตั้ง `LEGO_ALLOW_FRACTIONAL=false`,
   `python ops.py release-plan --env-file private-uat-wholeshare.env --account-id-stdin --window-sessions N --enforce` (ขั้นตอนเต็มใน `docs/CONTINUOUS_RELEASE_V4_TH.md` "ต่ออายุ release UAT")
2. deploy: `LEGO_ALLOW_FRACTIONAL_OVERRIDE=false EXPECTED_CANDIDATE_HASH=… LEGO_RELEASE_AUTHORIZATION_OVERRIDE=… LEGO_TRADING_WINDOW_END_OVERRIDE=… bash deploy/continuous-uat.sh`
   (`deploy/continuous-uat.sh` ใช้ `true` เมื่อไม่ตั้ง; `cloudshell-all-in-one.sh` ปฏิเสธค่าที่ไม่ใช่ boolean)
3. **ทางเดียวไม่ย้อนบน chain เดิม**: state ผูก `instrument_capability` (0.01 / 2 ตำแหน่ง); เปลี่ยนเป็น 1 / 0 ตำแหน่งเป็น "tightening" ที่ `lego_state` ยอมและ rebind ครั้งเดียว (`config_hash` ของ v2 ไม่รวม `allow_fractional` จึงไม่แตก chain),
   แต่ย้อนกลับเป็นเศษหุ้นถูกปฏิเสธ (`RuntimeIdentityError … lot-size migration` ⇒ ทุก tick ตัดสินใจไม่ได้). ถ้าต้องกลับต้องเปิด chain ใหม่ (เปลี่ยน DIFF หรือ DNA bundle) ซึ่งเริ่มนับ G1 ใหม่. `test_audit_20261020` ล็อกพฤติกรรมนี้
4. **ห้ามล้าง RTDB** (`webull_lego_state`, `webull_lego_rows`, `webull_lego_order_outbox`, trace) ระหว่างเก็บหลักฐาน G1 — 19 ต.ค. chain เริ่มใหม่ที่ v1 ทั้งที่ container boot ตั้งแต่ 16 ต.ค. ทำให้นับ session ย้อนหลังจาก export เดียวไม่ได้
5. อ่านผล: `python -m tools.trace_audit …` — whole-share ถูก `FILL` ⇒ H1 (เศษหุ้น) มีน้ำหนัก; ยัง `FAILED` ⇒ ส่งให้ Webull และหยุด reset ซ้ำ ๆ เมื่อ `failed-probe streak` ≥ 2 (ข้อ 13)

**เกณฑ์ canary เพิ่ม (S4)**: order tick ที่ตัดสินใจ+Place ใน tick เดียวเรียก broker ≈ 12 ครั้ง (instrument 1, positions 3, snapshot 3, open_orders 2, preview, place, order_detail) ใช้ 15–25 วินาทีบน UAT และ 3 จาก 7 order ต้องใช้ 2 tick
(เลื่อนที่ `require_budget` ก่อน fence — fail-closed ไม่เสียเงิน แต่ Preview ซ้ำ). ก่อนขยาย caps บน PROD ให้วัดสัดส่วน order ที่ต้องใช้ 2 tick และ p95 ของ order tick จาก `lego_operation` / `lego_tick_completed`; ถ้าแย่กว่า UAT ให้หยุดและตัดสินใจเรื่องลดการอ่านซ้ำก่อน (ไม่แก้ในรอบนี้ — เป็น money path)

## 15. order ที่ถูกเลื่อนก่อน Place และ tick ที่เลื่อนที่ decision stage (จาก audit 21 ต.ค.)

เหตุการณ์จริง 20 ต.ค.: BUY ทั้ง 2 ใบของ session ตายเป็น `NOT_PLACED` เพราะ `balances/get` ตอบ 417 `OPENAPI_SYSTEM_ERROR` 3 ครั้ง (14:00:33Z, 16:45:33Z; เหลือ ≈ 14 นาทีของ slot) — `docs/AUDIT_20261021_TH.md` S1.
ทั้งหมดนี้เป็น**การเลื่อน** ไม่ใช่การเปิดสิทธิ์: ไม่ต้องทำอะไรด้วยมือ และไม่มีอะไรให้ reset (ไม่มี fence, ไม่นับเป็น broker reject)

| อาการ | ความหมาย | ต้องทำอะไร |
|---|---|---|
| intent ค้าง `PENDING_DISPATCH` มี `gate_defer_count` 1 หรือ 2 และ `dispatch_check_phase=pre_place_read`; `last_error` เป็น `ServerException … 417` / 504 / timeout | gate อ่าน buying power หรือ Preview ล้มชั่วคราวก่อนถึง fence ของ Place; tick ถัดไปรัน gate **ทั้งชุดใหม่** (quote age, decision age, drift 100 bps, open orders, funding, execution limits) | ไม่ต้องทำอะไร. เลื่อนได้ ≤ 2 ครั้งต่อ intent และ ≤ 1 ครั้งต่อ tick; `business_status` เป็น `ERROR` ทุกครั้งที่พลาด (ได้ถึง 3 tick ต่อ intent — health-policy rate-limit 300 s จึงเป็น incident เดียว) |
| intent จบ `NOT_PLACED` ที่ `gate_defer_count` = 2 | อ่านล้มชั่วคราวติด 3 tick (≈ 2 นาที) — ผลเดียวกับก่อนแก้ แต่หลังลองใหม่แล้ว | เก็บ request id (`WEBULL_SDK_ERRORS` / `WEBULL_ERRORS` ของ `tools.trace_audit`) ส่งให้ Webull; ไม่ต้อง reset |
| intent จบ `SUPPRESSED_STATE_CHANGED` หลังเลื่อน | decision age เกินขีด (UAT 360 s, **PROD 120 s** ⇒ PROD เลื่อนได้ไม่เกินราว 2 tick) หรือราคา/ฝั่งเปลี่ยน | ปกติ — slot ถัดไปตัดสินใหม่ |
| tick `TICK_DEFERRED` โดยไม่มีแถวใหม่ และ trace มี `D08` + `T00` (`tick_deadline`) เมื่อเหลือ ≥ 1 s | decision stage เหลืองบ < 3 s หลัง snapshot (`DECISION_COMMIT_MIN_BUDGET_SECONDS`) จึงไม่เขียนแถว/circuit/intent; tick ถัดไปตัดสิน slot เดิมใหม่ด้วยงบเต็ม | ไม่ต้องทำอะไร; ถ้าเกิดต่อเนื่องหลาย tick แปลว่า `snapshots/list` ช้ามาก (20 ต.ค.: 15.4 s + 10.2 s ใน tick เดียว) — ส่ง request id ให้ Webull |
| `webull_token` warning บน UAT | หลัง build นี้ช่องว่าง ≈ 1.5 s ระหว่าง profile กับ pair ของ cache ไม่ทำให้เกิดอีก (เหลือเฉพาะ pair หมด TTL คั่นกลาง tick — เป็น warning ไม่ใช่ gate) ⇒ ถ้าเห็นซ้ำทุกชั่วโมงหรือบ่อยกว่านั้น ให้สงสัย identity/secret ที่เปลี่ยนจริง | ตรวจ `WEBULL_TOKEN_DIR` และ secret ตามข้อ 7; บน PROD token check เปิดอยู่เสมอ — warning นี้เป็นของจริง |

**เกณฑ์ canary เพิ่ม**: นับสัดส่วน intent ที่ถูกเลื่อน (`gate_defer_count` ≥ 1) ต่อ order ที่ถึง Place และสัดส่วนที่เลื่อนจนจบเป็น `NOT_PLACED` — บน PROD ถ้า `balances/get` ล้มบ่อยพอจะกินขีด decision age 120 s ให้หยุดและส่ง request id ให้ Webull ก่อนขยาย caps.
การเลื่อนไม่ผ่อนผัน guard ใด: ถ้าสงสัยว่า order ถูกส่งซ้ำหรือผิดฝั่ง ให้ดู `webull_lego_order_audit` (`dispatch_quote_check`) และ `webull_lego_execution_transitions` ก่อนสรุป
