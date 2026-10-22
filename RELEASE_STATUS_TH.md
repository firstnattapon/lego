# สถานะ candidate continuous execution v4

ล่าสุด 22 ตุลาคม: [audit guard อายุ quote ของ UAT กัน order ทุกใบเพราะ feed ของ sandbox ช้ากว่าเวลาจริงหลายชั่วโมง](docs/AUDIT_20261022_TH.md) — revision `00050`–`00054` (main@99000da หลัง PR #35), log 21 ต.ค. 13:23Z → 22 ต.ค. 04:22Z: 903 tick ↔ 903 request,
แถว AAPL 816–831 ต่อเนื่อง, สมการคำนวณใหม่จากหลักการเบื้องต้นตรงทั้ง 16 แถว, ไม่มี fill, candidate `a465c2d8…` ตรงกับ checkout สะอาดของ `99000da`. **ไม่มี order ถึง broker ทั้ง session**:
`READY_BUY` 22 slot (UBER 6, AAPL 16) จบ `SUPPRESSED_STATE_CHANGED` ด้วย `quote_age_limit` — snapshot ของ sandbox ตามหลัง 8,987 → 21,154 s เทียบเพดาน UAT 1,260 s; NFLX/MSFT ไม่ OC บน UAT (ERROR 45 tick);
00050 ล้มที่ instrument profile 15 tick ด้วยสาเหตุที่หาไม่ได้ (RTDB ถูกล้าง); deploy AAPL เป็น initial-funding release (2 order ต่อ session, window 2 session).
**แก้ใน source**: UAT ตัดสิน quote ด้วย session ที่เปิดอยู่ (ราคาปิดวันก่อนยังถูกกัน) ที่ทั้ง 5 จุดของ dispatch, PROD 60 s ไม่เปลี่ยน; tick log มี `state_change_reasons` และ `errors[].reason` (enum ของ instrument profile);
`trace_audit` เพิ่ม `DISPATCH_SUPPRESSED`; policy UAT กลับเป็น UBER (30 / 1,500 / 26). ไม่แตะสูตรตัดสินใจ/ledger/fence/recovery/circuit/caps. เทสต์ 2,165 passed / 9 skipped (+63, ไม่แก้เทสต์เดิม), mutation จับ 15/15.
ข้อจำกัดที่เปิดอยู่ (ไม่แก้ใน PR นี้): broker UAT อาจ `FAILED` order UBER ต่อ (15 ใบติด 16–20 ต.ค.); ไม่มี alert (G4); RTDB ถูกล้างอีกครั้ง (G1 นับไม่ได้).
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — ตรวจ halt/position ของ UBER ด้วย `ops.py status` แล้วออก release ใหม่ด้วยคำสั่งใน deploy.txt (`WINDOW_SESSIONS=10`) **ก่อน 2026-10-23T20:00Z** ที่ window ของ 00054 จบ;
เงินจริงยังต้อง alert ถึงคนจริง (G4), PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules PROD แยก, fee PROD ที่วัดแล้ว และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ.
ผล local และ evidence อยู่ใน `release_evidence/20261022-pr/`.

ล่าสุด 21 ตุลาคม: [audit gate ของ dispatch ทิ้ง BUY เพราะ `balances/get` 417 ชั่วคราว และ commit แถวโดยไม่เช็กงบ](docs/AUDIT_20261021_TH.md) — revision `00049-duk` (main@befa78c หลัง PR #31), log 20 ต.ค. 13:29–20:45Z: 437 tick ↔ 437 request (instance เดียว),
แถว 780–805 ต่อเนื่อง, สมการคำนวณใหม่จากหลักการเบื้องต้นตรงทั้ง 26 แถว, ไม่มี fill, candidate `d06333fe…` ตรงกับ checkout สะอาดของ `befa78c`. broker UAT ทำ SELL UBER 5 ใบ `FAILED` โดยไม่ให้เหตุผล
(รวม 15 ใบติดตั้งแต่ 16 ต.ค.; probe 2 ใบล้มทั้งคู่; halt ค้าง streak 2). **ข้อบกพร่องที่พบ**: (S1) BUY ทั้ง 2 ใบของ session (14:00 และ 16:45Z) ตายเป็น `NOT_PLACED` เพราะ `balances/get` ตอบ 417 ×3 ทั้งที่เหลือ ≈ 14 นาทีของ slot;
(S2) `TickDeadlineExceeded` ใน gate ถูกกลืนเป็น `NOT_PLACED` (ซ่อนอยู่ — reproduce ด้วยเทสต์); (S3) decision commit แถวโดยไม่เช็กงบ — v2 เหลือ 1.72 s จึงไม่มีสมการใน trace; (S4) `webull_token` warning ผิดที่ 16:45:07Z.
**แก้ใน source**: gate ไม่กลืน deadline และเลื่อน intent ≤ 2 ครั้งเมื่ออ่าน buying power/Preview ล้มชั่วคราว (guard ทุกตัวรันซ้ำทุกครั้ง); decision เลื่อนทั้ง tick เมื่อหลัง snapshot เหลือ < 3 s; `_token_check_disabled()` นับ pair ที่ยังไม่หมด TTL.
ไม่แตะสูตรตัดสินใจ/ledger/fence/recovery/circuit. เทสต์ 2,102 passed / 9 skipped (+47, ไม่แก้เทสต์เดิม), mutation จับ 24/24.
ข้อจำกัดที่เปิดอยู่ (ไม่แก้ใน PR นี้): สาเหตุ FAILED ฝั่ง Webull ไม่ทราบ (evidence pack อยู่ใน audit); ไม่มี alert (G4); chain ถูกล้างอีกครั้ง (G1 นับไม่ได้).
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — halt ที่ค้างเลือกได้สามทางก่อน 13:30Z (ปล่อยให้ probe ทำงาน / `reset-reject-halt` / `halt-orders`; ข้อ 0 ใน audit); PR นี้เปลี่ยน candidate hash จึงต้อง `release-plan` + `continuous-uat.sh` ใหม่ **ก่อน 2026-11-02T21:00Z**;
เงินจริงยังต้อง alert ถึงคนจริง (G4), PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules PROD แยก, fee PROD ที่วัดแล้ว และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ.
ผล local และ evidence อยู่ใน `release_evidence/20261021-pr/`.

ล่าสุด 20 ตุลาคม: [audit recorder ทิ้ง tick หลักฐาน และ halt ส่ง ERROR ทุก tick](docs/AUDIT_20261020_TH.md) — revision `00048-kap` (main@e039b48 หลัง PR #30), log 19 ต.ค. 13:30–20:47Z: 438 tick ↔ 438 request (instance เดียว 82 ชม. 5 นาที),
แถว 754–779 ต่อเนื่อง, สมการคำนวณใหม่จากหลักการเบื้องต้นตรงทั้ง 26 แถว, ไม่มี fill, candidate `eb7495ec…` ตรงกับ checkout สะอาดของ `e039b48`. broker UAT ทำ SELL UBER 7 ใบ `FAILED` โดยไม่ให้เหตุผล
(auto-probe ของ PR #30 ทำงานจริง 4 ครั้ง ล้มทุกครั้ง; streak 4, cooldown เพดาน 4 ชม. หมด 23:00:19Z). **ข้อบกพร่องที่พบ**: (S1) flight recorder ทิ้งทุก tick ที่จบเพราะงบหมด — สมการครบ 23/26 แถว, probe warning 2/4, Preview 7/9;
(S2) halt ของ broker เป็น ERROR 391/438 tick (health-policy จะ page ทุก ~5 นาทีทั้งคืนเมื่อผูก alert); (N3) whole-share control เป็นทางเดียวไม่ย้อนบน chain และ `continuous-uat.sh` รันไม่ได้.
**แก้ใน source**: tick ที่เป็นหลักฐานเขียนได้ถึงเหลืองบ 1 วินาที; halt ของ broker เป็น ERROR เฉพาะ 180 วินาทีแรกของทุกชั่วโมง (replay 391 → 32); `trace_audit` เพิ่ม `RECORDER_SKIPPED_TICKS`, `TRACE_GAP_ROWS`, `LOG_EXPORT_SHORT`;
`LEGO_ALLOW_FRACTIONAL_OVERRIDE` ใน `continuous-uat.sh`. ไม่แตะสูตรตัดสินใจ/ledger/fence/recovery. เทสต์ 2,055 passed / 9 skipped (+52), mutation จับ 29/29.
ข้อจำกัดที่เปิดอยู่ (ไม่แก้ใน PR นี้): สาเหตุ FAILED ฝั่ง Webull ไม่ทราบ (evidence pack อยู่ใน audit); probe จริงช้ากว่าออกแบบ 1 slot (N2); order tick = 12 broker call และ 3 จาก 7 order ต้องใช้ 2 tick (S4 — วัดบน PROD canary).
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — halt ที่ค้างเคลียร์ได้ด้วย `ops.py reset-reject-halt` **ก่อน 13:30Z** (ข้อ 0 ใน audit; redeploy ไม่เคลียร์ halt); PR นี้เปลี่ยน candidate hash จึงต้อง `release-plan` + `continuous-uat.sh` ใหม่ **ก่อน 2026-10-30T20:00Z**;
เงินจริงยังต้อง alert ถึงคนจริง (G4), PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules PROD แยก, fee PROD ที่วัดแล้ว และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ.
ผล local และ evidence อยู่ใน `release_evidence/20261020-pr/`.

ล่าสุด 16 ตุลาคม: [audit halt ที่ค้างหลัง FAILED 3 ใบติด](docs/AUDIT_20261016_TH.md) — revision `00047-qix` (main@11ecb6b หลัง PR #29), log 16 ต.ค. 13:30–15:04Z: 95 tick ทุก 60.0 วินาที ไม่มีช่องว่าง,
แถว 728–734 ต่อเนื่อง, สมการ/ledger/lineage 0 mismatch, ไม่มี fill, candidate `dd90b7d1…` ตรงกับ checkout สะอาดของ `11ecb6b`. **UAT หยุดเทรดที่เหลือของวัน**: SELL UBER 3 ใบติด (13:45 / 14:00 / 14:22Z) ถูก broker ทำ `FAILED`
โดยไม่ให้เหตุผล (Preview/Place/Detail ตอบ 200) → `BROKER_REJECT_HALT` (halt `12ae3645…`) ที่ไม่หายเอง ≈ 22 จาก 26 slot; ก่อนหน้านั้น `positions` 417 เป็นคลื่นฝั่ง broker (15 ต.ค. 14:57→19:59Z ≈ 20 slot, 16 ต.ค. 7 tick)
และไม่มี alert ถึงใคร. **แก้ใน source (UAT เท่านั้น)**: หลัง cooldown 900 s (×2 ต่อ probe ที่พลาด, เพดาน 4 ชั่วโมง) ปล่อย order ที่ READY อยู่แล้ว**ใบเดียว**ผ่าน halt — FAILED ⇒ halt กลับทันที, fill ⇒ เคลียร์; PROD ไม่เปลี่ยน (ต้องใช้คน);
`tools/trace_audit.py` เห็น halt ที่ค้างและ outage ของ decision; flight recorder บันทึกสถานะ circuit. ไม่แตะสูตรตัดสินใจ/ledger/fence/recovery/`max_mutations=1`. เทสต์ 2,003 passed / 9 skipped (+86), mutation จับ 56/56.
ข้อจำกัดที่เปิดอยู่ (ไม่แก้ใน PR นี้): zombie + cancel "ไม่ทราบผล" (F-b); สาเหตุ FAILED และ `positions` 417 ฝั่ง Webull ไม่ทราบ (evidence pack อยู่ใน audit).
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — halt ของวันนี้เคลียร์ได้ทันทีด้วย `ops.py reset-reject-halt` (ข้อ 0 ใน audit); PR นี้เปลี่ยน candidate hash จึงต้อง `release-plan` + `continuous-uat.sh` ใหม่ **ก่อน 2026-10-29T20:00Z**;
เงินจริงยังต้อง alert ถึงคนจริง (G4), PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules PROD แยก, fee PROD ที่วัดแล้ว และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ (DIFF=25 ที่ fee 1.07% ≈ −547 USD/ปี ต่อ FIX_C 10,000 ในแบบจำลอง).
ผล local และ evidence อยู่ใน `release_evidence/20261016-pr/`.

ล่าสุด 15 ตุลาคม: [audit UAT 2 session สะอาด](docs/AUDIT_20261015_TH.md) — revision `00045-luv` (main@0fffe10 หลัง PR #28), session 13–14 ต.ค.: 52/52 แถว, สมการ/ledger/fill/cashflow คำนวณซ้ำ 0 mismatch
(426 tick ↔ 426 request, 26 commit event ↔ 26 แถว), 14 intents = FILLED 8 · CANCELLED 3 · EXPIRED 1 · FAILED 1 · SUPPRESSED 1; ไม่มี halt/fence ค้าง. **กฎ UAT PENDING (PR #28) ใช้จริงครั้งแรก**: SELL 1.07 ค้าง `PENDING` ถูกปล่อย `EXPIRED` ที่ +31 นาที
ระหว่าง session โดยไม่ halt (guard ของ order ถัดไปยังไม่เห็นในข้อมูล — export จบก่อน session 15 ต.ค.). ที่เหลือ: broker `FAILED` ไม่ให้เหตุผลครั้งที่ 2 (SELL 0.38), ไม่มี alert (ERROR 1 tick ไม่ถึงใคร), fee UAT 1.074% ต่อ order,
quote UAT หน่วง ~903 s (PROD เพดาน 60 s), cancel timeout 1 ใน 4 ครั้ง และ window จบ **2026-10-26T20:00Z**. PR นี้ไม่แตะ money path/recovery: webhook ครอบ status ที่ policy ครอบ + payload `text`/`content` + `ops.py alert-drill`;
`tools/measure_fee.py` (Preview-only); `trace_audit` วัดอายุ quote (`QUOTE_NOT_REALTIME` เมื่อ PROD); runbook หัวข้อเส้นทางที่ UAT ไม่เคยทดสอบและขั้นตอนเมื่อ UAT หยุดเพราะ order ค้าง.
ข้อจำกัดที่เปิดอยู่ (ไม่แก้ใน PR นี้): zombie + cancel "ไม่ทราบผล" ⇒ halt ที่ไม่มีเครื่องมือปล่อยจนกว่า broker retire order (ไม่เคยเกิด; ดู F-b ใน audit).
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — PR นี้เปลี่ยน candidate hash จึงต้อง `release-plan` + `continuous-uat.sh` ใหม่ **ก่อน 2026-10-26T20:00Z**; เงินจริงยังต้อง alert ถึงคนจริง (G4),
PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules PROD แยก, fee PROD ที่วัดแล้ว และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ. ผล local และ evidence อยู่ใน `release_evidence/20261015-pr/`.

ล่าสุด 13 ตุลาคม: [audit order UAT ค้าง PENDING *และยัง list เป็น open* ซ้ำ](docs/AUDIT_20261013_TH.md) — revision `00044-nac` (main@51462e2), session 12 ต.ค.: สมการ/ledger/lineage ตรงทั้ง 26 แถว
(439 tick ↔ 439 request, คำนวณซ้ำด้วยเลขคณิตธรรมดา 0 mismatch) แต่ BUY 1.37 ค้าง `PENDING`, cancel ถูก 417, **เสีย 23 จาก 26 slot** แล้ว halt ข้าม session (รอบที่ 4 ใน 6 session).
**ต้นเหตุที่ `cancel_expire` ไม่ปล่อย order ทราบแล้ว**: flight recorder (21:00Z และ 22:07Z) แสดง `order_detail`=PENDING filled 0 และ `open-orders/list` ยังคืน order นี้ ⇒ `order_still_listed_open` — UAT ไม่เอา order ที่ค้างออกจาก open list.
แก้ใน source (เฉพาะ UAT; PROD และทางที่คนยืนยันบน PROD ได้ proof เดิม): `order_recovery.py` ปล่อยเป็น `EXPIRED` zero-fill เมื่อ order ยัง `PENDING` filled 0 ทั้งใน order detail และ open orders (แถวเดียว ขนาดตรง) holdings ไม่ขยับ
และอายุ ≥ 30 นาทีนับจาก Place ระหว่าง session (ไม่ต้องรอปิดตลาด) · `execution_service.py` guard ตอน dispatch ทั้งก่อนและหลัง Preview **ข้าม order ที่ปล่อยแล้ว** ตราบที่ broker ยังบอก PENDING filled 0 (ตรวจรอบสองพบว่าปล่อย fence อย่างเดียวไม่ทำให้เทรดต่อ:
`SUPPRESSED_ACTIVE_ORDER` จะบล็อกต่อ) · `tools/resume_order_reconciliation.py` กู้ order ที่ halt อยู่แล้ว (`basis` อยู่ใน confirmation) · ฟิลด์ `expiry_basis` · `trace_audit` finding `UAT_STUCK_PENDING_RELEASED`.
ไม่แตะสูตรตัดสินใจ/ledger/fence/halt/`max_mutations=1`. เทสต์ 1,862 passed / 9 skipped (+72), mutation ถูกจับ 18/18 (ตรวจรอบสามพบและแก้ bug ที่เครื่องมือกู้เทียบ label กับ `"UAT"` ซึ่งจะทำให้ขั้นตอนกู้ล้ม; พบด้วยว่า RTDB ถูกล้างบาง path ก่อน 12 ต.ค. — chain เริ่ม genesis ใหม่ ให้ผู้ปฏิบัติการยืนยัน). ความเสี่ยงที่เหลือ: zombie ที่ปล่อยแล้วอาจ fill ทีหลัง (ledger ระบุที่มาไม่ได้; ไม่เคยเห็นใน 3 ตัวก่อนหน้า) — บอกใน audit.
สถานะ: **code-ready, live-evidence-pending (NO-GO เงินจริง)** — order ที่ halt อยู่ต้องกู้ด้วยคน (ขั้นตอนใน audit รวมทางลัดวันนี้ที่ไม่ต้อง deploy); PR นี้เปลี่ยน candidate hash จึงต้อง `release-plan` + `continuous-uat.sh` ใหม่ **ก่อน 2026-10-21T20:00Z**;
เงินจริงยังต้อง alert ถึงคนจริง (ERROR 392 tick + halt ไม่ถึงใคร), PROD observe ≥2 session + quote real-time, token ที่คนหมุน, DB/rules แยก, fee PROD ที่วัดแล้ว (UAT 1.07% ต่อ order) และการตัดสินใจเชิงเศรษฐศาสตร์ของเจ้าของ.
ผล local และ evidence อยู่ใน `release_evidence/20261013-pr/`.

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
