# สถานะ candidate continuous execution v4

ล่าสุด 10 ตุลาคม: [audit UAT session แรกหลัง flight recorder](docs/AUDIT_20261010_TH.md) — revision `00043-tux` เทรดครบ 19 slot, fill 5 ครั้ง; ledger, สมการ (คำนวณซ้ำอิสระ),
cash ของ fills และ lineage ของ log (311 request ↔ 311 tick, 19 แถว ↔ 19 commit event) ตรงทั้งหมด และไม่มี order ค้าง PENDING รอบนี้. ที่เหลือ: order `44142ee4…` ถูก broker `FAILED` โดยไม่ให้เหตุผล
(`tools.readiness_audit` fail `snapshot_integrity` — ส่งหลักฐานให้ Webull ตามเอกสาร), ไม่มี alert channel, และ **ต้นทุน**: fee UAT 1.07% ต่อ order กินผลตอบแทน — กลยุทธ์ถือ ≈FIX_C ดอลลาร์คงที่ จึงไม่มี edge ในตัว
เมื่อราคาเป็น random walk; ที่ความผันผวนที่วัดได้ (≈17%/ปี) กำไรก่อน fee ในโลกที่ดีที่สุดเพียง ≈0.8% ของ FIX_C ต่อปี. แก้ใน source (ไม่แตะ recovery/halt/ledger/fence/สูตรตัดสินใจ):
retry หนึ่งครั้งตอนสร้าง SDK client (503 ที่ 18:45), เก็บส่วนท้ายของ tick ที่ส่ง order ใน flight recorder, กัน `WEBULL_API_DEBUG` บน PROD, PROD ต้องระบุ `DATABASE_URL_OVERRIDE` (+ `database.rules.prod.json`),
`release-plan --fee-pct` และ WARN เฉพาะ PROD live (fee ยังไม่วัด, ไม่มี webhook), `trace_audit` แสดงขนาด order เทียบ fee และชี้ order ที่ FAILED ไม่มีเหตุผล.
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — UAT ทำงานต่อได้จน window สิ้นสุด **2026-10-23T20:00Z**; PR นี้เปลี่ยน candidate hash จึงต้อง `release-plan` + `continuous-uat.sh` ใหม่
**ก่อน 2026-10-21T20:00Z** (ต่ออายุ window และได้ผลแก้); เงินจริงยังต้อง alert ถึงคนจริง, PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules แยก, fee PROD ที่วัดแล้ว และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ.
ผล local และ evidence อยู่ใน `release_evidence/20261010-pr/`.

ล่าสุด 9 ตุลาคม: [audit order UAT ค้าง PENDING ซ้ำ](docs/AUDIT_20261009_TH.md) — รอบที่ 3 ใน 4 session; `cancel_expire` ไม่ปล่อย order 21:00–23:22Z
**โดยไม่มีหลักฐานว่า proof ติดเงื่อนไขไหน** (เหตุผลหายใน `logger.info` ที่ Cloud Logging ไม่รับ) จึงเพิ่ม [flight recorder](docs/FLIGHT_RECORDER_TH.md)
(`flight_recorder.py`: ผัง LEGO + สมการ + คำตอบ Webull + ทุกการกระทำ ต่อ tick ใน RTDB private; `webull_io.py`/`tick_runtime.py`: request id, HTTP status, error code)
และ `tools/trace_audit.py` (digest ของ logs + RTDB + YAML + repo, คำนวณสมการซ้ำ) — ไม่แตะ recovery/halt/ledger/fence. สมการของ 52 แถวและ 5 fills ที่ export มาคำนวณซ้ำตรงทั้งหมด.
สถานะ: **code-ready, live-evidence-pending (NO-GO)** — สาเหตุจริงที่ proof ไม่ผ่านยังไม่ทราบ (หาได้ด้วย dry run ในขั้นตอนกู้ order ของ audit); order ที่ค้างและ halt ต้องกู้ด้วยคน;
ยังไม่มี alert delivery จริง, PROD observe หรือ edge หลังหัก fee (fee UAT ≈1.07% ต่อ order, realized −1.534 USD). ผล local และ evidence อยู่ใน `release_evidence/20261009-pr/`.

ล่าสุด 7 ตุลาคม: [audit order UAT ค้าง PENDING เกินปิดตลาด](docs/AUDIT_20261007_TH.md) — hold 8 ชั่วโมงของรอบ 6 ตุลาคม
หมดแล้ว halt ข้าม session เพราะ UAT ไม่เคย terminal order ที่ broker ปฏิเสธ cancel (สมมติฐาน "DAY order terminal ก่อนปิดตลาด" ผิด)
และไม่มีเครื่องมือปล่อย order non-terminal; แก้ใน `order_recovery.py` (`cancel_expire`: ปล่อยเป็น EXPIRED เฉพาะ UAT เมื่อพิสูจน์ครบ,
`tools/resume_order_reconciliation.py --expiry-proof` ให้คนยืนยัน) และ `webull_io.py` (PROD ยังอ่าน/reconcile ได้เมื่อ token
เหลือน้อยกว่า margin 3 วัน โดย order ใหม่ยังถูกบล็อก). ผล local และ evidence อยู่ใน `release_evidence/20261007-pr/`.
สถานะ: **code-ready, live-evidence-pending (NO-GO)** — order ที่ค้างอยู่ต้องกู้ด้วยคนหลัง deploy (ขั้นตอนใน audit);
fill จริงแล้ว BUY 0.86 / SELL 0.51 แต่ยังไม่มี alert delivery จริง, PROD observe หรือ edge หลังหัก fee (fee UAT ≈1.08% ต่อ order).

ล่าสุด 6 ตุลาคม: [audit incident UAT หยุดเทรด 3.5 ชั่วโมง](docs/AUDIT_20261006_TH.md) — broker ปฏิเสธ cancel
(HTTP 417 `OPENAPI_ORDER_CANNOT_OPERATE`) แต่ระบบนับเป็น "ไม่รู้ผล" แล้ว halt เงียบ; แก้ใน `order_recovery.py` (hold 8 ชั่วโมง
แล้ว poll ต่อ) และ `observability.py` (halt ต้อง page). ผล local และ evidence ของ PR นี้อยู่ใน `release_evidence/20261006-pr/`.
สถานะ: **code-ready, live-evidence-pending (NO-GO)** — order ที่ค้างอยู่ต้องกู้ด้วยคนก่อนเปิดเทรดต่อ (ขั้นตอนใน audit);
ยังไม่มี BUY fill, alert delivery จริง, PROD observe หรือ fee PROD ที่วัดแล้ว.

ก่อนหน้า 5 ตุลาคม: [audit release horizon/caps และเส้นทางเงินจริงแบบ acknowledgement](docs/AUDIT_20261005_TH.md)
และ [runbook PROD live](docs/PROD_LIVE_RUNBOOK_TH.md). ผล local และ candidate ของ PR นี้อยู่ใน
`release_evidence/20261005-pr/`. สถานะ: **code-ready, live-evidence-pending (NO-GO)** — ยังไม่มี UAT BUY/SELL fill,
endurance สอง session, alert delivery จริง, PROD observe สอง session หรือ token ที่ rotate โดยคน.
`ops.py release-plan` ตรวจ window/DNA/caps ก่อนทุก release; ไม่มีอะไรต่ออายุเอง.

ก่อนหน้า 30 กันยายน: [audit ฉบับ final และ gate ที่ค้าง](docs/AUDIT_20260930_FINAL_TH.md).
ผล source/PR รอบนั้นอยู่ใน `release_evidence/20260930-pr/`; acceptance ด้านล่างเป็น
หลักฐานย้อนหลัง ไม่ใช้รับรอง candidate ใหม่. UAT ต่อเนื่อง/เงินจริงยังต้องผ่าน live gates.

**NO-GO สำหรับเงินจริง; UAT endurance ยัง BLOCKED**. ผล local candidate และ external gates อยู่ใน [final review](docs/implementation_plan_review.md) และ [acceptance JSON](release_evidence/continuous-v4-acceptance.json). Candidate นี้ยังไม่ได้ deploy หรือส่ง broker mutation จริง. Production rollout ต้อง observe/inactive.

## หลักฐานย้อนหลังวันที่ 24 กันยายน 2026

**ยังไม่ผ่านเกณฑ์เปิดเงินจริง (NO-GO)** เพราะยังไม่มีหลักฐาน broker และ deployed-candidate proof ครบตาม release gates. โค้ดแก้ v3 open-orders cursor, grouped legs, audit pairing และ fence/accounting safeguards แล้ว แต่ต้องยืนยันกับ Webull และ environment จริงก่อนเปิด Place ใน Production

## หลักฐานย้อนหลัง 17 กันยายน 2026

**ยังไม่ผ่านเกณฑ์เปิดเงินจริง (NO-GO)**

แก้ account identity validation, เพิ่ม instrument/buying-power smoke,
เพิ่ม broker diagnostics ครบ 9 routes และแก้ SDK log ซ้ำ/credential redaction
ชุดทดสอบระบบ: **872 passed, 4 skipped** บน Python 3.12
อ่าน [รายงานล่าสุด](docs/AUDIT_20260917_TH.md)
HTTP 417 ในหลักฐานเกิดบน SDK 3.0.1 route ปัจจุบันแล้ว; ต้องพิสูจน์ broker recovery
และ deployed candidate ก่อนเปิดเงินจริง ผลย้อนหลังด้านล่างไม่รับรอง candidate ใหม่นี้

## หลักฐานย้อนหลัง 15 กันยายน

แก้ local: open-orders fail-closed, ตรวจ slot ซ้ำก่อน broker I/O, log error/DNA headroom,
และรวม shell deploy script ใน candidate manifest
ทดสอบใน environment แยกพร้อม Firebase Emulator: **778 passed, ไม่มี skip**
ยังไม่ได้ deploy source ที่แก้หรือทดสอบกับ broker จริงในงานนี้

อ่าน [รายงาน audit และเกณฑ์เปิดเงินจริง](docs/AUDIT_20260915_TH.md)
candidate ใหม่และหลักฐานอยู่ใน `.audit-cache/20260915/`; สร้าง manifest ใหม่ก่อน deploy
ผลและ candidate ในรายงานครั้งที่ 3 เป็นหลักฐานย้อนหลัง ไม่ใช่ผลรับรอง source ปัจจุบัน
