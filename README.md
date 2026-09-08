# LEGO PRINCIPAL v2

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

## Local verification

```powershell
python -m pytest -q
python ops.py check
```

ไม่มีคำสั่ง local ใดใน repo นี้ส่ง order เอง การ deploy, UAT Place และ Production
canary ต้องได้รับ authorization ที่มี environment/account/symbol/side/quantity/วงเงิน
กำกับใน session ที่ลงมือจริงเสมอ
