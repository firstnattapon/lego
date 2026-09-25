# การแก้ audit สำหรับการเทรดต่อเนื่อง

ขอบเขต: F1, F3–F6 จาก incident audit วันที่ 12 กันยายน 2026 โดยไม่เปลี่ยน DNA bundle, bypass, origin หรือหลักการหมดอายุของ F2 ตามคำขอผู้ใช้

## พฤติกรรมหลังแก้

| เรื่อง | พฤติกรรมและหลักฐานที่ทดสอบ |
|---|---|
| ค่าธรรมเนียม | อ่าน actual commission + actual fees แบบ nested ตามเอกสาร TH/global พร้อมรองรับ scalar ของ endpoint เก่า ไม่ใช้ receivable/estimated เป็น actual ไม่แปลง missing/invalid เป็นศูนย์ |
| การกู้คืน fill | late fee ทำให้ ledger และ terminal fence ปิดได้; การ reconcile ซ้ำไม่เพิ่ม quantity/cashflow ซ้ำ เมื่อ fee ยังไม่ทราบต้องรอและแจ้ง overdue หลัง 15 นาที |
| Audit | intent ใหม่และทุก status transition มี marker; suppressed/expired สะท้อน audit; revision และ compare-and-set acknowledgment ป้องกัน mirror เก่าเขียนทับ/ล้างงานซ่อมใหม่ |
| เวลา Place | `placed_at` เกิดเมื่อบันทึก Place attempt แบบ durable เท่านั้น; เวลา decision/preview แยกจาก Place attempt |
| Deadline | tick มี budget 35 วินาทีเทียบ Cloud Run 45 วินาที; retry ตรวจเวลาที่เหลือ; SDK request ตั้ง connect/read timeout ตาม budget; Secret Manager มี timeout; RTDB HTTP timeout 5 วินาที; ก่อน Place ต้องเหลือเวลา; deadline ไม่กิน reconcile-failure counter |
| งานซ้ำ | tick ไม่ query order เดิมซ้ำใน dispatch phase เมื่อ recovery ยังมี unresolved result |
| Business health | ทุก HTTP return มี structured event `lego_tick_completed` พร้อม correlation/revision/candidate, slot/run, execution/fee status; dispatch exception ไม่ตอบ TICK_OK/200; HTTP 200 ที่รอ fee มี business status แยก |
| Token | ใช้ boolean `token_check_enabled` ที่ broker ตอบจริงและผูกกับ credential/endpoint/cache TTL; ไม่มี token file อย่างเดียวไม่สรุปว่า 2FA เสีย; ลด warning ซ้ำเป็นเมื่อเปลี่ยนหรือทุก 10 นาที |
| Production preflight | typed runtime ใช้ release binding ที่ตรง deployment; legacy gate ยังเป็น UAT เท่านั้น Production ที่ไม่มี binding ยังถูกปฏิเสธ |
| Deployment identity | สร้าง manifest ได้เมื่อ checkout มี backend อย่างเดียว; deploy ปฏิเสธ CandidateHash ที่ไม่ตรง source; audit-cache ไม่ถูกส่งขึ้น runtime |

เอกสาร fee: [Webull Thailand Order Detail](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md), [Webull global Order Detail](https://developer.webull.com/apis/docs/reference/order-detail.md)

**ข้อจำกัดเรื่อง deadline:** เป็นการหยุดเริ่มงานและจำกัด I/O ไม่ใช่การ kill thread หรือรับประกันเวลาจริงแบบ hard real-time การ transaction retry, DNS และ platform termination ยังต้องพิสูจน์ใน deployed fault-injection/soak test กลไก fence จึงยังคงอยู่หลังผล Place ไม่ชัดเจน

## การตรวจใน PR

`test_continuous_runtime.py` ครอบคลุม nested/zero/invalid/late fee, replay, audit repair/race, SDK timeout, auth profile, HTTP/log correlation และ Production binding

```powershell
python -m pytest -q -rs
python tools/candidate_manifest.py
```

CI เดิมรัน `test_database_rules.py` ภายใต้ Firebase Emulator ซึ่งรวม rules matrix และ subprocess probes สองตัว: `emulator_race_probe.py`, `emulator_tick_race_probe.py` แต่ละตัวใช้ real RTDB Emulator กับ 16 workers และ stub เฉพาะ broker; ไม่ส่งคำสั่งซื้อขายจริง ผล local/CI ของ commit สุดท้ายต้องอ่านจาก PR checks ไม่ใช้ผลของ candidate เก่ารับรอง candidate ใหม่

## อัปเกรดและกระทบยอด incident เดิม

1. เก็บ export/outbox/audit ของ environment ที่จะอัปเกรด ตรวจสถานะ actual order detail ของ run เดิมผ่าน account ที่ถูกต้อง ไม่ส่ง Place ซ้ำเพื่อแก้ fee
2. ใช้ source commit และ dependency ที่ผ่าน CI สร้าง candidate manifest ใหม่และ release binding สำหรับ account/environment นั้น deploy โดยค่า mode/active ตามขอบเขตที่ operator อนุมัติ
3. Recovery จะอ่าน fee ใหม่เอง ถ้า response มี actual breakdown ครบต้องเห็น `FILLED`, `broker_fee_status=KNOWN`, cashflow/realized ครบ และ fence ถูกปลด ถ้ายังไม่ครบจะเห็น `WAITING_BROKER_FEE`/`FEE_OVERDUE`; ตรวจ broker schema และ fee จริง ห้ามใส่ศูนย์แทนค่าที่ไม่ทราบ
4. Audit เก่าที่ไม่มี marker ใช้คำสั่งด้านล่างทีละ run ภายใต้ runtime identity และ chain เดิม ตรวจ dry-run ก่อน apply คำสั่งนี้เปลี่ยนเฉพาะ marker/mirror ไม่เปลี่ยน execution state หรือส่ง broker request

```powershell
python ops.py status
python ops.py repair-audit --run-id <RUN_ID>
python ops.py repair-audit --run-id <RUN_ID> --apply
```

สถานะใน decision row เป็นประวัติการตัดสินใจ ต้องดูร่วมกับ execution audit/outbox; READY ที่ถูก preflight block อาจไม่มี intent โดยถูกต้องตาม guard

## Monitoring ที่ต้องเชื่อมบน deployment

ใช้ Cloud Logging query โดยเปลี่ยนชื่อ service ให้ตรง environment:

```text
resource.type="cloud_run_revision"
resource.labels.service_name="lego-tick-uat"
jsonPayload.event="lego_tick_completed"
```

สร้าง alert ไปยังช่องทางของ operator สำหรับ `severity=ERROR`, `business_status=FEE_OVERDUE`, `MANUAL_RECONCILIATION_REQUIRED`, `OUTBOX_RECOVERY_PENDING`, และ repeated `WAITING_RECONCILIATION` ตรวจ absence ของ completion event เทียบ scheduler heartbeat และตรวจ state ไม่เดินระหว่าง active market session แยกจากตลาดปิด/paused

อย่าใช้ HTTP 200 เป็น trading health หรือใช้ยอด warning เก่าที่สะสมเป็นเหตุเสียปัจจุบัน ใช้ structured tick result ล่าสุดร่วมกับสถานะ outbox; log มีเฉพาะ allowlisted fields ไม่รวม raw broker request/response, account ID, token หรือ signature

## เกณฑ์รับรอง Production ที่ยังต้องใช้หลักฐานจริง

PR/code และ green tests เป็นหลักฐานเฉพาะขอบเขตที่ทดสอบ **ยังไม่ใช่ใบรับรอง Production ทั้งระบบ** ก่อนเปิดใช้งานต้องมีหลักฐานผูกกับ candidate เดียวกันดังนี้:

- Deployed image/build source ตรง candidate, IAM/OIDC และ RTDB rules ที่ใช้งานจริงถูกต้อง; access policy ของ public read nodes เป็นที่ยอมรับ
- Webull UAT ของ account/SDK/endpoint ที่จะใช้: preview → Place ที่อนุมัติ → terminal quantity/price/actual fees → holdings/cash reconciliation → repeat reconciliation โดยไม่มี duplicate order
- Timeout/crash/rollback ระหว่าง unresolved intent และการคืนระบบหลัง cold start/token rotation; ทดสอบ late fee/partial fill/cancel/reject ตาม contract จริง
- Soak ตาม workload ที่ตั้งใจใช้: ไม่มี backlog/fee pending ที่ไม่แจ้งเตือน; latency, API quota และ RTDB cost อยู่ในเกณฑ์ที่ operator กำหนด; alert ถูกส่งถึงผู้รับและทดลอง recovery/restore สำเร็จ
- ตรวจ pagination ของ endpoint จริงก่อนอัปเกรด SDK: adapter ปัจจุบันเรียก `order_v3.list_order_open(..., pagination_key=...)` และเดินหน้าต่อจาก cursor ที่ response คืนมา แต่ยังต้องพิสูจน์ multi-page scan กับบัญชีและ endpoint TH UAT ที่จะ deploy จริง

ยังไม่มี live deployment/broker validation ของ candidate ใน PR นี้ จึงไม่ควรเปลี่ยนข้อความ release เป็น Production certified จนหลักฐานข้างต้นครบ การแก้ F2 หรือการเพิ่มอายุ DNA ไม่อยู่ใน PR นี้
