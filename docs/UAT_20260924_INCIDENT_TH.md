# UAT 24 กันยายน 2026: ผล audit และเกณฑ์ปลด money fence

สถานะของหลักฐานชุดนี้: **BLOCKED / NO-GO สำหรับเงินจริง** รายงานที่อ่านได้ด้วยเครื่องอยู่ที่ [`../release_evidence/UAT_20260924_READINESS.json`](../release_evidence/UAT_20260924_READINESS.json) ผลนี้เป็นการตรวจ export และ log ที่ให้มา ไม่ใช่การอ่านสถานะ broker หรือ deployment ปัจจุบัน

## ขอบเขตหลักฐาน

| ชุดข้อมูล | การผูกหลักฐาน | สิ่งที่ตรวจพบ |
|---|---|---|
| Cloud Logging export 24 ก.ย. | SHA-256 `6e309e21831e833e5efcceb670fd7018216f51d03c6971cb57139b243f8b2619`; revision `lego-tick-uat-00030-kuw`; candidate `d54c3bddf70a92f3ba29befdec358204dcf6e9307b9c42f6a0e50e9e6b40cd8a` | 131 tick จับคู่ request ด้วย trace/revision/เวลา/HTTP ได้ 131/131; committed decision ใน log ตรงกับแถว RTDB 10/10; business status `MANUAL_RECONCILIATION_REQUIRED` 124, `WAITING_RECONCILIATION` 4, `ERROR` 3 |
| RTDB export 24 ก.ย. | SHA-256 `2275e059ba5081047b6fd26c12f4b6d4cb127a36fc7c6902d97eb527a76fd7c1` | 5 intents: `EXPIRED_UNSENT` 4, `FILLED` 1; audit mirror ตรง 5/5; มี money fence ค้าง 1 |
| Intent ที่ `FILLED` | outbox, payload, broker cashflow และ ledger ใน export เดียวกัน | payload quantity `0.31721` แต่ cumulative filled quantity `0.320000`; fee เป็น `KNOWN` และ broker cashflow บันทึกแล้ว แต่ `cashflow_finalized=false`, `cashflow_abandoned=true`, `needs_manual_check=true`; ไม่มี model/realized witness ที่ครบ |

ความต่าง `0.00279` หุ้นเป็น **ความไม่สอดคล้องที่ต้องสอบสวน** ยังไม่มี raw broker Order Detail, execution legs, account position/cash activity และ preview ที่ตรงกันพอจะระบุว่าเกิดจาก broker, SDK serialization, rounding หรือข้อมูล snapshot จึงห้ามปรับจำนวนหุ้นหรือปลด fence จาก export อย่างเดียว รายงาน UBER 30 นาทีทดสอบ SDK โดยตรง ส่วนแผน TSLA 45 นาทีไม่ได้รัน Place ตามรายงานของมันเอง; ทั้งสองชุดไม่รับรอง candidate LEGO ที่ deploy

## สิ่งที่ patch นี้ทำ

- Canonical status รองรับ `CANCELED` และ `CANCELLED` สำหรับ zero/partial fill ทั้ง runtime และ offline audit
- ยืนยัน client order ID, symbol, side และจำนวนใน payload ตรงกับ committed intent ก่อน Preview; ถ้าต่างกันหยุดก่อน Place
- หลัง durable Place marker ใช้ intent จาก transaction ที่ยืนยันแล้วเพื่อตรวจ cumulative fill; fill เกินจำนวนที่ส่งหรือ payload/identity ไม่ชัดเจนจะคง manual money fence และไม่ลง ledger อัตโนมัติ
- คำสั่ง admin dry-run/ack สำหรับ positive fill ตรวจ durable Place marker, submitted payload/intent quantity, broker cashflow event และยอดเงิน/ค่าธรรมเนียมเทียบ exact broker detail ก่อนปลด fence; หาก witness เปลี่ยนระหว่าง dry-run กับ apply จะปฏิเสธ. เคส `0.31721` กับ `0.320000` ยังไม่ผ่าน guard นี้แม้ภายหลังมี ledger entries จนครบ
- แยก `broker_reason_missing` จากเหตุ reject จริง; ข้อความ fallback ไม่อ้างว่า broker ส่งเหตุผลมา
- Offline audit ตรวจ committed row ต่อ intent และ log, missing terminal quantity, payload/intent mismatch, overfill, identity mismatch, แถวที่ยังไม่ execute แต่ขยับ ΔA, แถว `FINALIZED` ที่ ΔA/A/E ไม่ตรง model witness, ledger witness ที่ไม่ตรงกัน และ reject reason gap โดยเก็บเฉพาะยอดรวมและ hash ของ source ในรายงาน

## ขั้นตอนปิด incident ก่อนเปิดคำสั่งใหม่

1. คงการสร้างคำสั่งใหม่ของ chain ที่มี fence ไว้ แต่ให้ read-only reconciliation ทำงานต่อ เก็บ source export, log, revision, image digest และ release config ที่ตรวจซ้ำได้
2. ดึง exact Order Detail และ execution events ด้วย client order ID เดิมจากบัญชีและ endpoint ที่ถูกต้อง พร้อม open/history ทุกหน้า, position, cash activity และ actual fees ณ เวลาเดียวกัน เก็บ raw evidence ในที่จำกัดสิทธิ์และ hash ทุกชุด
3. เทียบ submitted quantity, order quantity, cumulative fill, execution legs, fee, broker cashflow, realized ledger, model ledger และ decision row ทีละรายการ หากแยก transaction อื่นในบัญชีไม่ได้ ให้คง `BLOCKED`
4. กรณี positive fill ที่ `cashflow_finalized=false` ห้ามใช้ `lego_admin_reconcile` ปลด fence โดยตรง: เครื่องมือนี้ต้องการ finalized witness อยู่แล้ว ต้องมี repair protocol สำหรับ ledger ที่ idempotent และทดสอบ crash/replay บน Firebase Emulator ก่อนใช้กับ incident นี้
5. หลังซ่อม ตรวจ broker และ RTDB ใหม่; ต้องพิสูจน์ว่าไม่มี active/unknown order, ledger ทุกชุดตรงกับ broker, audit mirror สด, fence ถูกปลดตาม protocol และ alert ส่งถึง operator แล้ว จึงเริ่ม UAT soak บน candidate เดียวกัน

เอกสาร Webull ที่ใช้ตรวจ contract: [Order Detail](https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md), [Open Orders](https://developer.webull.co.th/apis/docs/reference/trade-api/order-open.md), [Stock Trading](https://developer.webull.co.th/apis/docs/trade-api/stock/). ผล local test หรือ PR ไม่แทน broker/deployment evidence และไม่อนุญาตให้เปิดเงินจริง
