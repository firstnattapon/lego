# UAT 5 ตุลาคม 2026: SELL 1.53175 ที่ broker แสดงเป็น 1.530000 และ money fence ที่ค้าง

สถานะ: **แก้ในโค้ดแล้ว (local tests) — ยังไม่ deploy, ยังไม่ปลด fence, ยังไม่มีหลักฐาน broker ใหม่** ข้อมูลด้านล่างอ่านจาก RTDB export ที่ผู้ใช้ส่งมา (`lego-firebase-default-rtdb-export.json`, เก็บแบบ private) ไม่ใช่การอ่าน broker หรือ deployment ปัจจุบัน และ local test ไม่พิสูจน์ว่า broker รับหรือแสดงปริมาณ 2 ตำแหน่งตามที่สมมติไว้

## เหตุการณ์ (UTC, 2026-10-05)

| เวลา | เหตุการณ์ |
|---|---|
| 13:45:14Z | chain `UBER_33b541c2034c` step 495 `READY_SELL` จำนวน 1.53175 ที่ราคา 68.68 (holdings 147.13455) run `f327df93cec10d5180b7d9db6255759f` |
| 13:45:22–27Z | Preview ผ่าน (estimated cost 106.15, fee 1.14) แล้ว Place MARKET/DAY ด้วย payload `quantity: "1.53175"` broker order `0388C9VQEK80O0KF4Q5C000000` |
| 13:45:31Z | อ่าน Order Detail: `status=PENDING`, `total_quantity="1.530000"`, `filled_quantity="0.000000"` → `order_recovery.validate_evidence` โยน `BrokerContractAnomaly` (ส่ง 1.53175 แต่ broker รายงาน total 1.53) |
| 13:45:31Z | intent เป็น `MANUAL_RECONCILIATION_REQUIRED` + `needs_manual_check`; `system:order-recovery` ตั้ง operator halt `fd412e8376b4407da0b4eca77d5945b4` (reason `BrokerContractAnomaly`); fence ของ account/UBER ค้าง |
| 14:00:11Z | tick ถัดมาได้ `PASS_OPERATOR_HALT` (step 496) และอ่าน holdings ได้ 145.6028 |

สิ่งที่อ่านเพิ่มได้จาก export:

- แถว step 495 ยังเป็น `PENDING_EXECUTION` และ `execution_cashflow` ยัง `finalized_seq=0`, `actual_cumulative=0` — ledger ของโมเดลยังไม่บันทึกการขายนี้
- holdings ลดจาก 147.13455 เป็น 145.6028 = ลด **1.53175 พอดี** ไม่ใช่ 1.53 ที่ Order Detail แสดง จึงยังสรุปไม่ได้ว่า broker ดำเนินการจำนวนไหน
- รูปเดียวกับ 24 ก.ย. (payload 0.31721, cumulative filled 0.320000): [UAT_20260924_INCIDENT_TH.md](UAT_20260924_INCIDENT_TH.md)

## สาเหตุในโค้ด

instrument profile ของ Webull ไม่มี field ความละเอียดของปริมาณ `parse_instrument_capability` จึงไม่ได้อ่านค่านี้ และ `InstrumentCapability` ใช้ค่าคงที่ 5 ตำแหน่ง (`MAX_FRACTIONAL_DECIMAL_PLACES`, increment 0.00001) เป็น default `decision_service.run_decision` เอาค่านี้ตั้ง `cfg.quantity_increment/decimal_precision` ให้ engine และ `build_order_payload` ทุกขั้นจึงทำงานที่ 5 ตำแหน่ง ส่วน `validate_evidence` ปฏิเสธ broker total ที่ไม่เท่ากับ payload ที่ส่ง (ตั้งใจ — fail closed)

"broker ใช้ 2 ตำแหน่ง" เป็น**สมมติฐานจาก 2 เหตุการณ์** (ปัดใกล้สุดทั้งสองครั้ง) ไม่ใช่เอกสารของ broker การส่งแค่ 2 ตำแหน่งปลอดภัยทั้งสองกรณี: ถ้า broker ทำที่ 2 ตำแหน่ง payload กับ total ตรงกัน ถ้า broker ทำที่ 5 ตำแหน่ง ปริมาณ 2 ตำแหน่งก็ยังถูกต้อง

## สิ่งที่ patch นี้ทำ

- `webull_io.py`: `DEFAULT_FRACTIONAL_DECIMAL_PLACES = 2` และ increment default 0.01 (ใช้เมื่อ capability ไม่ระบุ precision); `MAX_FRACTIONAL_DECIMAL_PLACES = 5` ยังเป็นเพดานของ validation เพื่อให้ intent และ state เดิม (5 ตำแหน่ง) โหลดได้; fractional gate เทียบกับ `capability.decimal_precision` ไม่ใช่เพดาน
- `lego_state.py`: `commit_final_row` เดิมโยน `RuntimeIdentityError` เมื่อ capability ใน state (5 ตำแหน่ง) ไม่ตรงกับค่าใหม่ ซึ่งจะทำให้ทุก commit เป็น `CONFIG_ERROR` และ DNA clock หยุด (ทางเลือกเต็มหุ้นก็ชน guard เดียวกัน) ตอนนี้ยอมเฉพาะ**การแคบลง**: ตำแหน่งน้อยลง และ increment ใหม่เป็นจำนวนเต็มเท่าของเดิม (เทียบด้วย Decimal จาก shortest repr เพราะ state เก็บ `1.0000000000000001e-05` ซึ่ง 0.01 หารแล้วได้ 999.99… ไม่ใช่ 1000) การขยาย (2→5) หรือที่ไม่สัมพันธ์กันยังโยน error เหมือนเดิม; commit แรกที่ผ่านบอกใน response เป็น `instrument_capability_migrated_from` ครั้งเดียว
- `lego_broker_smoke.py`: Preview ใช้จำนวนตำแหน่งของ capability (ไม่ใช่ `LEGO_DECIMAL_PRECISION` default 5) และปฏิเสธปริมาณที่ละเอียดกว่านั้นแทนการตัดทิ้งเงียบ ๆ
- ไม่ผ่อน `validate_evidence`, admin reconcile หรือ `_holdings_match_fill` (tolerance 1e-6) และไม่เพิ่ม env ใหม่ — `chain_key`/`config_hash` ของ `_v2` ไม่รวม precision chain เดิมจึงต่อเนื่อง
- test: `test_fractional_quantity_incident.py` (ใช้ค่าและสตริงจริงจาก export), ปรับ `test_fractional_shares.py` และ `test_broker_smoke.py`

หลักฐาน local: `pytest` 1,532 passed / 9 skipped (ก่อนแก้ 1,464 / 9); ด้วย RTDB emulator 1,541 passed ทั้งหมด; มี test ที่ replay เหตุการณ์นี้ผ่าน `run_decision` และ `_run_order_worker` จริง (broker เป็น double ที่รายงานปริมาณกลับที่ 2 ตำแหน่ง): สัญญา 5 ตำแหน่งได้ anomaly เหมือนเดิม สัญญา 2 ตำแหน่งไม่เกิด

## ลำดับที่ต้องทำ (ห้ามสลับ)

1. **หลักฐาน (read-only, ผู้ใช้ทำ)**: Order Detail ของ order ข้างบน (status, filled_quantity, avg price, fee, execution legs), position UBER ปัจจุบัน และ cash activity ช่วง 13:45Z — ตัดสินว่าบัญชีต้องบันทึก 1.53 หรือ 1.53175 ห้ามแก้ RTDB, ห้ามลบ halt/fence ด้วยมือ, ห้าม Place ใหม่ด้วย ID ใหม่
2. **Release (ผู้ใช้อนุมัติ)**: candidate hash ใหม่ → `python ops.py release-plan` → `deploy/continuous-uat.sh` (ต้องมี `LEGO_TRADING_WINDOW_END_OVERRIDE` ใหม่; สคริปต์ปฏิเสธระหว่าง regular session) deploy ตอนไม่มี intent ที่ dispatch ได้ค้างจาก revision เก่า เพราะ worker ส่ง intent ด้วย `strategy_config` ที่บันทึกไว้ในตัว intent (ยัง 5 ตำแหน่ง) ไม่ใช่ค่าใหม่
3. **ตรวจ tick แรกที่ commit หลัง deploy**: ต้องไม่เป็น `CONFIG_ERROR`, response มี `instrument_capability_migrated_from`, และ `webull_lego_state/<chain>/instrument_capability` เป็น `quantity_increment` `0.01` **และ** `decimal_precision` 2 ทั้งคู่ ถ้า increment ไม่ใช่ `0.01` แปลว่า profile ของ Webull ระบุ increment มาเอง (repo ไม่มี profile response จริงที่บันทึกไว้ และ patch นี้ไม่รู้ว่ามี field นี้หรือไม่): ค่า default 2 ตำแหน่งจะคู่กับ increment ที่ละเอียดกว่า engine จะคำนวณที่ increment นั้น แต่ payload ตัดเป็น 2 ตำแหน่ง dispatch จึงปฏิเสธ (`order payload identity/quantity differs from committed intent`) intent จบ `NOT_PLACED` โดยไม่ส่ง order ให้หยุดและตัดสินใจก่อนปลด halt
4. **เคลียร์ order ที่ค้าง**: ถ้าไม่ fill/ยกเลิก/reject ใช้ `lego_admin_reconcile.py --chain-key UBER_33b541c2034c --run-id f327df93cec10d5180b7d9db6255759f` (dry-run ก่อน) ถ้า fill แล้ว (คาดว่าส่วนใหญ่ — holdings ลดแล้ว) **ยังไม่มีเครื่องมือที่ปล่อยเคสได้**: `tools/resume_order_reconciliation.py` เรียก `validate_evidence` ซ้ำ (anomaly เดิม) และ admin reconcile ต้องการ cashflow/model finalization ที่ยังไม่เกิด ต้องทำ positive-fill ledger repair protocol เป็นงานแยกพร้อมรีวิวเข้ม หลังเห็นหลักฐานข้อ 1
5. **ปลด halt**: `python ops.py clear-operator-halt --halt-id fd412e8376b4407da0b4eca77d5945b4 --operator <ผู้รีวิวคนละคนกับผู้ตั้ง> --reason ...` — **หลัง deploy และเคลียร์ order เท่านั้น** ถ้าปลดก่อน tick ถัดไปจะส่ง order 5 ตำแหน่งจาก revision เก่าและเกิด anomaly ซ้ำ (เครื่องมือปฏิเสธเองเมื่อ fence ยังมี order ค้างหรือผู้ปลดเป็นคนเดียวกับผู้ตั้ง แต่ไม่ตรวจว่า deploy แล้วหรือยัง)
6. **พิสูจน์ด้วย order fractional แรกหลังแก้**: ค่าสี่ตัวต้องเท่ากัน — `order_payload.quantity`, `reconcile_evidence.total_quantity`, `filled_quantity` และ position delta (`holdings_after = decision_holdings − quantity` ภายใน 1e-6) ถ้าไม่เท่า สมมติฐาน 2 ตำแหน่งผิด: หยุด ไม่ปลดต่อ แล้วทบทวนเป็นเต็มหุ้น (`LEGO_ALLOW_FRACTIONAL=false`, precision 0) ซึ่งผ่าน guard ข้อเดียวกันนี้ (0 < 2)

## Rollback และข้อควรระวัง

หลัง commit แรกของข้อ 3 state ผูกกับ contract 2 ตำแหน่ง revision เก่า (5 ตำแหน่ง) จะถูก guard ปฏิเสธ: tick เป็น `CONFIG_ERROR` (HTTP 500), ไม่มีการส่ง order จึงไม่มีความเสี่ยงเงิน แต่ DNA clock หยุดเพราะไม่ commit (และทุก tick ที่ถูกปฏิเสธทิ้งแถว `committed=false` ค้างใน `webull_lego_rows` — พฤติกรรมเดิมของ guard ตัวนี้ ไม่ได้มาจาก patch) และใน repo ไม่มีเครื่องมือ migrate กลับ จึงแก้ไปข้างหน้า (roll forward) ตามนโยบาย rollback เดิมที่ไม่ย้อนกลับไป binary ที่ไม่รู้จักสถานะใหม่ ([CONTINUOUS_RELEASE_V4_TH.md](CONTINUOUS_RELEASE_V4_TH.md))

## ที่ patch นี้ไม่ได้ทำ

- ไม่ซ่อม intent/ledger ของ order `f327df93…` (ต้องใช้หลักฐานข้อ 1 และ protocol แยก)
- ไม่ปลด halt/fence และไม่ deploy
- ไม่พิสูจน์ความละเอียดปริมาณที่ broker ใช้จริง จนกว่าจะผ่านข้อ 6
- ยังไม่มี alert เมื่อ halt ที่ระบบตั้งเอง (`system:order-recovery`) ค้างนาน: `observability.severity_for` ให้ `MANUAL_RECONCILIATION_REQUIRED` ที่ `reconciliation_paused` เป็น INFO จึงเงียบ (ข้อเสนอแยก ไม่ได้ทำในรอบนี้)
