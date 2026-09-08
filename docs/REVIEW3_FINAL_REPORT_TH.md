# ผลตรวจการบ้านครั้งที่ 3 และโค้ดส่งมอบ

Candidate ที่ผู้ใช้ส่งมา `7704b57d60679b24a1fccd130fb02a925f969d3f41064291b6c8bb8421d3b22d`
ยังมีข้อบกพร่องสำคัญ 2 จุด แม้ชุดทดสอบเดิมผ่าน 706 tests จึงไม่รับรองคำกล่าวว่า local ครบในสภาพเดิม
ตรวจพบและยืนยันทั้งสองจุดด้วย RTDB emulator จริง ก่อนแก้ไขและทดสอบซ้ำ

**สถานะหลังแก้: LOCAL_COMPLETE=true (10/10), FRAMEWORK_READY=false (16/25, BLOCKED 9)**

- Candidate ส่งมอบ: `03c36ba8667252a39e6b355226847bb1c5a94ee93e83614bbf89c8d03d716034`
- Dependency identity: `fe75d7030e885d61f5458ae81b6c0430c6a5d834edddd9e287746c65c8c91a90`
- ตรวจเสร็จ UTC: 2026-09-07T08:04:28.219314+00:00
- ครอบคลุม source/config/tests/docs/vendor wheel ของ backend และ reader รวม 100 ไฟล์
- ไม่มี deployment, คำขอถึง broker จริง หรือ order จริงในงานนี้; Place ที่ทดสอบเป็น stub เท่านั้น

## ข้อค้นพบและการแก้

| ข้อค้นพบ | ก่อนแก้ | โค้ดส่งมอบและหลักฐาน |
|---|---|---|
| R3-01 / P1: cumulative fill เพิ่มระหว่าง FIFO ค้าง | SELL 10 หุ้นกำลัง match แล้ว broker รายงาน 12 หุ้น ทำให้ worker เป็น REALIZED_MATH_ERROR และต้องตรวจมือ | ทำ checkpoint เดิมให้เสร็จก่อน บันทึก witness จาก input ที่ใช้จริง จากนั้นค่อยรับ delta ใหม่; คง matching pending และเลื่อน model finalization จนตามข้อมูล terminal ทัน |
| R3-02 / P1: rollback ชน migration finalize | rollback ลบ page ที่อีก worker เพิ่งเชื่อมกับ schema v3 ทำให้ FIFO อ่าน page ไม่พบ | claim/cancel migration epoch แบบ transaction; แยก page generation; ไม่ลบ page ตอน rollback; ปฏิเสธ rollback ถ้า finalize ชนะ และ stale writer เปิดใช้ checkpoint ที่ยกเลิกไม่ได้ |
| R3-03 / P2: รายงานขอบเขตอ่านหน้าไม่ตรง | ตัวเลข 8 เดิมนับ cursor advance ไม่รวม projection-refill reads | วัดการอ่าน Reference.get จริงบน emulator: ไม่เกิน 8 ขั้นจับคู่ แต่ได้สูงสุด 16 direct page reads/call; อ่านตรง key ไม่มี history-sized fetch |
| R3-04 / P2: หลักฐานและชุดส่งมอบไม่ครบ | generator เดิมระบุ PASS/count ไว้ตายตัว; manifest reader มีเพียง 3 ไฟล์ ไม่รวม dna_engine และ requirements | generator ใหม่ตรวจ candidate, exit code, hash ของ raw logs; manifest รวม reader ทั้งชุดและ CI/docs; มี regression ปฏิเสธ candidate ผิด, log ถูกแก้, command ขาด และ command ล้มเหลว |

ไฟล์หลัก: `lego_state.py` ส่วน migration/rollback และ `apply_realized_fill`;
`test_round3_final_repairs.py`, `tools/round3_regression_probe.py`,
`tools/emulator_fifo_stress.py`, `tools/build_round3_evidence.py`, `tools/candidate_manifest.py`.

## ผลตรวจ candidate ใหม่

| การตรวจ | ผลจริง |
|---|---|
| Backend | 717 passed, 1 skipped in 21.75s |
| Reader | 55 passed in 7.89s |
| RTDB rules emulator | 68 passed; การ skip ใน backend คือ emulator matrix ซึ่งรันแยกแล้ว |
| lego_tick บน RTDB emulator | 16 workers → 1 row, 1 intent, 1 stub Place |
| FIFO stress | 100 / 1,000 / 10,000 lots; seeded hot head 616 / 617 / 618 bytes; head หลัง matching 1,088 bytes |
| Work bound ที่วัดจริง | 65 lots ใช้ 9 calls; direct reads [16,16,16,16,16,16,16,16,2]; ไม่เกิน 8 cursor advances/call |
| Growing fill / fee / VWAP / replay | ผ่านใน worker path บน emulator; model finalize stub ถูกเรียก 1 ครั้งเฉพาะ terminal และ 0 ครั้งขณะ pending |
| Migration interleavings | finalize ก่อน rollback, cancel ระหว่างเขียน, stale checkpoint และ restart ด้วย source ใหม่ ผ่าน; linked pages อ่านจับคู่ต่อได้ |
| pip check / compile / secret scan | ผ่าน; secret scan เป็น heuristic บน source ใน manifest ไม่ใช่การรับประกันว่าไม่มี secret ทุกชนิด |
| pip-audit | No known vulnerabilities found |

ทดสอบ backend ใน environment แยก `.venv-round3-clean` ที่มีอยู่เดิมและตรวจ requirements ซ้ำแบบ offline;
reader ใช้ Python environment เดิมที่เวอร์ชันตรงกับ requirements. ไม่มีการอ้างว่าได้สร้าง clean reader environment ใหม่สำเร็จ
ในการตรวจครั้งนี้. การติดตั้ง reader environment ใหม่ครั้งแรกติดข้อจำกัดเครือข่าย จึงใช้ environment ที่ตรวจเวอร์ชันแล้ว
และรันทั้ง 55 tests ซ้ำ. การ audit ติดต่อเฉพาะฐานข้อมูลช่องโหว่สาธารณะ หลังเพิ่ม timeout จึงผ่าน

## การใช้งานและ migration

- โค้ดใน workspace ถูกแก้เรียบร้อยแล้ว ZIP เป็น source delivery รวม backend, reader, tests, docs, dependency specifications และ vendored wheel
- แตก ZIP จะได้โฟลเดอร์ `lego-firebase-main` และ `lego-firebase-streamlit` อยู่ระดับเดียวกัน
- Backend: ติดตั้ง `requirements-dev.txt` ใน virtual environment แล้วรัน `python -m pytest -q`
- Reader: ติดตั้ง `requirements.txt` และ pytest ในอีก environment แล้วรัน `python -m pytest -q`
- Emulator: เริ่ม RTDB emulator สำหรับ demo project แล้วรัน `python tools/verify_final_local.py --child emulator`
- ดู `QUICKSTART_TH.md`, `CONFIG_MIGRATION.md` และ `ADR_BOUNDED_FIFO_V3.md` สำหรับวิธีปฏิบัติ
- schema v3 เดิมที่ไม่มี generation ยังอ่านได้; rollback ก่อน finalize ยกเลิก epoch และเก็บ orphan pages ไว้
  การลบ orphan pages ไม่ใช่งานของคำสั่ง rollback นี้; หลัง finalize ต้องใช้โค้ดที่รองรับ v3

## ขอบเขตที่ยังไม่ได้ทำ

A02, A17–A23 และ A25 ยัง BLOCKED: cloud inventory/IAM/deployment, Webull UAT/PROD,
cloud performance/cost และ live migration/cutover. A25 คือผลรวมทุก gate จึงไม่ผ่านเมื่อ live gates ยังขาด
ไม่ได้อ้างว่า production พร้อม และไม่มีการเปลี่ยน BLOCKED ให้เป็น PASS โดยใช้ emulator แทน live evidence

## แหล่งหลักฐาน

- `FINAL_VALIDATION.json`: command, cwd, เวลา, exit code และ SHA-256 ของ raw log แต่ละชุด
- `LOCAL_CLOSURE.json`, `ACCEPTANCE.json`, `RELEASE_MANIFEST.json`: สถานะและ identity ของ candidate ส่งมอบ
- `REVIEWER_ROUND3_EMULATOR_PROBE.json`: หลักฐานข้อผิดพลาดก่อนแก้บน emulator
- `baseline_7704/`: เก็บเอกสารคำกล่าวเดิมไว้ ไม่ใช่สถานะ candidate ปัจจุบัน

Dependency identity เปลี่ยนเพราะเพิ่ม reader requirements เข้า hash map ไม่ได้เปลี่ยน dependency pins
และ candidate hash เปลี่ยนทั้งจากการแก้โค้ดและการขยาย source coverage

ไฟล์เดิมที่เปลี่ยน (9):
- `backend/ADR_BOUNDED_FIFO_V3.md`
- `backend/CONFIG_MIGRATION.md`
- `backend/lego_state.py`
- `backend/QUICKSTART_TH.md`
- `backend/test_round2_repairs.py`
- `backend/tools/build_round3_evidence.py`
- `backend/tools/candidate_manifest.py`
- `backend/tools/emulator_fifo_stress.py`
- `backend/tools/migrate_realized_fifo_v3.py`

ไฟล์ใหม่หรือเพิ่งเพิ่มเข้าการครอบคลุม manifest (16):
- `backend/LEARNING_GUIDE_TH.html`
- `backend/test_round3_final_repairs.py`
- `backend/tools/round3_regression_probe.py`
- `backend/tools/verify_final_local.py`
- `backend/.github/workflows/ci.yml`
- `reader/.gitignore`
- `reader/dna_engine.py`
- `reader/requirements.txt`
- `reader/test_dash_core.py`
- `reader/test_execution_confirmed_contract.py`
- `reader/test_freeze_policy.py`
- `reader/test_merged_core.py`
- `reader/test_streamlit_smoke.py`
- `reader/docs/dna_resonate_local_minimum_5_step_plan.html`
- `reader/docs/dna_resonate_local_minimum_5_step_plan.json`
- `reader/.github/workflows/ci.yml`
