# implementation_plan_review — Final code

แผน audit วันที่ 27 กันยายน 2026; ส่งมอบ candidate ใน PR นี้. **ยัง NO-GO สำหรับเงินจริง และยังไม่รับรอง UAT ต่อเนื่อง**. การทดสอบในเครื่องไม่แทนหลักฐาน broker/deployment จริง.

## สิ่งที่เปลี่ยน

| งาน | Implementation | หลักฐานตรวจรับใน repository |
|---|---|---|
| A Deadline | detail หลัง Place ≤1 ครั้ง, budget checks, safe TICK_DEFERRED, phase/witness telemetry, clock fixture ที่ควบคุมได้ | continuous/runtime/incident/operator-halt tests |
| B Recovery | cancel SDK v3 ครั้งเดียว, durable policy/count/deadline, grace halt, late reconciliation, history pagination ≤7 days, คง fence จน ledger/audit ผ่าน | recovery v4, accounting regression และ emulator_cancel_probe |
| C Daily/release | XNYS market-day quota, conservative dry-run/apply migration, release binding v4 ใช้ร่วมกับ deploy, intent schema guard | execution limits, recovery/operations v4 tests |
| D Operations | durable PROD token preflight, DNA/release warnings, private immutable transition audit, Monitoring renderer, captured deployment verifier, daily comparison report | token tests, rules/emulator, operations v4 tests |
| E Documentation/evidence | UAT/PROD profiles, runbook, corrected study guide, CSV และ acceptance JSON | ไฟล์เอกสารชุดนี้และ release_evidence |

ผล pytest จำนวนจริงและ hashes อยู่ใน [`continuous-v4-acceptance.json`](../release_evidence/continuous-v4-acceptance.json) พร้อม JUnit จาก full suite ที่เปิด Emulator. รายการ PASS หมายถึงเฉพาะ scope ที่ระบุ. รายงานไม่อ้างว่ามีการ deploy, Place/Cancel กับ broker จริง หรือเปิดเงินจริงในงานนี้.

## ข้อสรุป audit ที่ยังใช้ได้

Snapshot/log เดิม: 878 entries, 430 ticks, HTTP 429×200/1×503; 26 rows/outbox/mirrors เชื่อมโยงกัน; 24 EXPIRED_UNSENT, 1 SUPPRESSED_STATE_CHANGED, 1 CANCELLED. ไม่มี positive fill. ณ export ไม่พบ unresolved fence แต่ยังไม่ทราบ broker ปัจจุบัน. Baseline local suite เดิม 1095 passed/1 clock-dependent failure/8 emulator skipped ไม่ใช่ผล candidate ใหม่นี้.

แก้ข้อสรุปจาก reviews 1–8: 503 ก่อน placed_at จึงยังระบุ phase ต้นเหตุไม่ได้; ไม่ยืนยันว่า fractional ทำให้ค้าง; ไม่ยืนยัน Scheduler retry/duplicate จาก 503. Cancel ack ไม่ปลด fence, ห้าม unlimited mutation retry, token rotation อาจต้อง human verification. คง budget 35/45s แทนข้อเสนอเดิม 50/60s. Cloud Monitoring เป็นช่องทางหลัก ไม่บังคับ webhook.

## เกณฑ์ที่ยังไม่ปิด

| เกณฑ์ | สถานะ | หลักฐานที่ต้องเพิ่ม |
|---|---|---|
| Live cutover, current broker scan, backup และ migration | BLOCKED | Cloud/broker access, fresh private snapshots และช่วงอนุมัติใหม่ |
| UAT สอง regular sessions | BLOCKED | UBER/fractional profile, cold restart, daily rollover, BUY/SELL positive fill และ ledger closure |
| UAT fractional/whole-share control | NOT RUN | ทดสอบตามลำดับหลัง fence ว่าง; ไม่แข่ง account-symbol |
| Actual revision/image/config/traffic/retry verification | NOT RUN | Captured deployment provenance ของ candidate นี้ |
| Alert delivery, halt/resume, invalid token/secret drills | NOT RUN | Cloud Monitoring incident/channel/received evidence จริง |
| PROD observe/inactive สอง regular sessions | NOT RUN | Authenticated read-only state, clock, token, monitoring และไม่มี new orders |
| Live activation | BLOCKED | Separate production release authorization พร้อม caps/daily count/approval window |

หาก UAT ไม่ให้ positive fill ให้คง BLOCKED. ห้ามแทนด้วย mock/cancel-only PASS. ขั้นตอนจริงดู [runbook](CONTINUOUS_RELEASE_V4_TH.md); ความหมาย decision/submitted/filled และ initial funding ดู [คู่มือ](LEARNING_CONTINUOUS_V4_TH.md); แผนแก้ไขที่ปรับแล้วอยู่ใน [CSV](implementation_plan_review.csv).
