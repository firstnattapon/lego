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
| Alert | apply `tools/monitoring_config.py`, ตั้ง `ALERT_WEBHOOK_SECRET_OVERRIDE`, ทดสอบถึงผู้รับจริง (G4) |
| PROD observe | ≥ 2 regular sessions: authenticated reads, quote สด, ไม่มี new-order mutation; ยืนยันสิทธิ์ market data real-time |
| Token | operator-issued, `NORMAL`, เหลือ > 24 ชม. (runtime ไม่ส่ง order ใหม่ถ้าเหลือน้อยกว่านั้น), ตั้งแผน rotate ก่อน 15 วัน |
| แยกจาก UAT | account/secrets `*-prod`, service `lego-tick-prod`, RTDB แยก (`DATABASE_URL_OVERRIDE`; script ไม่บังคับให้แยก — คุณต้องตรวจเอง) |
| ต้นทุน | วัดค่าธรรมเนียมจริงด้วย Preview Order เทียบ `LEGO_DIFF` ว่าคุ้มหรือไม่ |

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
      --window-sessions 5 --reference-price <ราคาล่าสุด> --recommended-limits --enforce > private-plan.json
# release funding (วิธี B รอบแรก): เพิ่ม --initial-funding
```

- `--window-sessions N` สิ้นสุดที่ปิดตลาดของ session ที่ N (ต้องครอบ ≥ 2 session สมบูรณ์); canary แนะนำช่วงสั้น เช่น 5 session
- `--recommended-limits` ให้ notional 10% ของ principal, orders 10 ต่อ session, quantity = 1.3 × notional ÷ ราคา ปัดขึ้นเป็นเลขกลม — เป็นจุดเริ่ม
  ตัดสินใจเองได้ แต่ notional > 25% ของ principal เป็น BLOCK; DNA ต้องจบหลัง window (เตือนถ้าเหลือ < 2 session)
- `--enforce` exit 1 เมื่อมี BLOCK; ผลลัพธ์เป็น private: มี release authorization และ ack; ไม่พิมพ์ account id
- PROD เริ่มด้วย whole shares (`LEGO_ALLOW_FRACTIONAL=false`) — `true` บน PROD เป็น WARN ให้ทบทวนอย่างชัดเจน
- candidate hash คำนวณจาก checkout ที่ commit แล้วและสะอาด (script ปฏิเสธ working tree ที่ไม่สะอาด)

## 3. Deploy

ทำ**นอกเวลา regular session เท่านั้น** (09:30–16:00 New York) — script ปฏิเสธระหว่าง session เพราะ smoke tick ท้าย script เป็น tick ปกติ

```bash
export WEBULL_ENV_OVERRIDE=PROD LEGO_MODE_OVERRIDE=trade LEGO_ACTIVE_OVERRIDE=true
export WEBULL_TOKEN_SECRET_OVERRIDE=projects/lego-firebase/secrets/<token-secret>
export ALERT_WEBHOOK_SECRET_OVERRIDE=<secret ที่เก็บ URL ปลายทาง>
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

- `verify_deployment` ตรวจ `production_mode`: observe/inactive หรือ trade/active ที่มี ack, authorization และ caps ครบ — ไม่ได้ตรวจว่าค่าถูกต้องต่อบัญชี
- `business_status=RELEASE_UNAUTHORIZED` (severity ERROR, `operational_health.orders_blocked_by_release=true`) = deployment เป็น trade/active แต่ไม่ส่ง order เพราะ release authorization หรือ `LEGO_PROD_LIVE_ACK` ไม่ตรงกับ release (มักเกิดจากการแก้ env ของ function ภายหลัง) — `python ops.py check` แสดง `release_authorized`, `prod_live_gate_open`, `new_orders_authorized`; แก้ด้วยการ deploy ใหม่ผ่าน script ไม่ใช่แก้ env ทีละค่า
- เฝ้า tick แรกของ session ถัดไปด้วยตัวเอง: `business_status`, `release_expiring`, order แรกและ fill ตรงกับ caps ที่อนุมัติ
- วิธี B: ทันทีที่ funding fill ยืนยันและ holdings ตรง broker ให้ออก release steady (caps แน่น, `--initial-funding` ออก) — อย่าปล่อย release funding ไว้

## 5. Canary

symbol เดียว, whole shares, caps ต่ำ, window สั้น, เฝ้าหลาย session แรกด้วยตัวเอง กำหนดเงื่อนไขหยุดล่วงหน้า:
`needs_manual_check`, `order_contract_anomaly`, `OPEN_ORDER_BLOCKED` ที่ไม่หาย, holdings ใน state ต่างจาก broker, broker reject circuit เปิด
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

token ของ PROD ออกผ่านขั้นตอน Webull ที่รองรับ (ต้องยืนยันตัวตนในแอป) ไม่ต่ออัตโนมัติ — เตือนเมื่อเหลือ ≤ 7 วัน และ runtime หยุดส่ง order ใหม่เมื่อเหลือ ≤ 24 ชม.

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
