# LEAPS แทนหุ้น (`LEGO_LEAPS`)

ถือ LEAPS (deep ITM call) แทนหุ้นบางส่วน แล้วให้ LEGO นับมันรวมกับหุ้นตอน rebalance
`LEGO_LEAPS` = จำนวน**หุ้นจำลอง** (1 สัญญา = 100 หุ้นจำลอง) ค่าเริ่มต้น `0` = พฤติกรรมเดิมทุกบิต

## 1. กฎคำนวณ

| ขั้น | สูตร / กฎ | โค้ด |
|---|---|---|
| หุ้นจริง | `real` = จำนวนหุ้นจาก Webull positions (แถว `OPTION` ถูกข้ามอยู่แล้ว ไม่นับ LEAPS ซ้ำ) | `webull_io._extract_qty` |
| ตำแหน่งที่ rebalance | `effective = real + LEGO_LEAPS` (บวกแบบ Decimal ไม่เสีย precision เช่น 151.3228 + 100 = 251.3228) เกิดก่อนคำนวณทุกอย่าง | `lego_one_row.effective_holdings`, `build_decision` |
| มูลค่า/ส่วนต่าง | `Vₙ = effective × Pₙ`, `gap = Vₙ − FIX_C` | `build_decision` |
| BUY | ซื้อหุ้นจริงตาม "จำนวนสั่ง (หุ้น)" ปกติ | `build_decision` |
| SELL | ขายได้จากหุ้นจริงเท่านั้น: เพดาน = `real` (ปัดลงตาม precision ของ broker) | `build_decision` |
| SELL เกินหุ้นจริง | ถ้า `|gap| / P ≥ real` หรือจำนวนหลังปัด `≥ real` → **PASS** สถานะ `PASS_SELL_EXCEEDS_REAL` จำนวนสั่ง 0 ไม่ตัดเหลือ เหตุผลในคอลัมน์ "เหตุผล" เช่น `PASS_SELL_EXCEEDS_REAL: ต้องขาย 30 หุ้น ≥ หุ้นจริง 5 หุ้น — LEAPS 100 หุ้นจำลองขายเป็นหุ้นไม่ได้ จึงไม่ส่ง order (ไม่ตัดเหลือ)` | `_sell_exceeds_real` |
| Rₙ/ΔAₙ/Aₙ/Eₙ | สูตรเดิมไม่เปลี่ยน (scale ด้วย FIX_C ไม่มี holdings เป็น input) LEAPS มีผลผ่านการตัดสินว่าแถวไหนเทรดเท่านั้น | ไม่แตะ `compute_recurrence` / `finalize_recurrence` |

ทำไม SELL ที่ "เท่ากับ" หุ้นจริงก็ PASS: ขายหมดทำให้หุ้นจริงเป็น 0 ซึ่ง guard position หาย
(`check_holdings_continuity`) ตีความว่า broker อ่าน position หาย (ตามดีไซน์) แล้ว chain จะค้าง

ตัวอย่าง (FIX_C 3,000, DIFF 5, LEAPS 100):

| ราคา | หุ้นจริง | effective | Vₙ | gap | ผล |
|---|---|---|---|---|---|
| 20 | 10 | 110 | 2,200 | −800 | READY_BUY 40 หุ้นจริง |
| 40 | 5 | 105 | 4,200 | +1,200 | ต้องขาย 30 > 5 → `PASS_SELL_EXCEEDS_REAL` |
| 20 | 50 | 150 (FIX_C 2,500) | 3,000 | +500 | READY_SELL 25 (< 50 หุ้นจริง) |
| 40 | 0 | 100 | 4,000 | +1,000 | LEAPS อย่างเดียวเกินเป้า → `PASS_SELL_EXCEEDS_REAL` ทุกแถว |

ถ้า `LEAPS × ราคา ≥ FIX_C` จะ PASS ทุกแถวที่ต้องขาย เพราะ LEAPS อย่างเดียวก็เกินเป้าแล้ว — ตั้ง FIX_C ให้มากกว่ามูลค่า LEAPS เสมอ

ช่วงราคาที่เทรดได้ (`release_horizon.leaps_price_band`, audit 3 พ.ย.):
- BUY ได้เมื่อ P < (FIX_C − DIFF) / LEAPS
- SELL ได้เมื่อ P < FIX_C / LEAPS ไม่ว่าถือหุ้นจริงเท่าไร (need = LEAPS + R − FIX_C/P < R ⇔ P < FIX_C/LEAPS)
- ตั้งแต่ FIX_C / LEAPS ขึ้นไปไม่มี order เลย
- ตัวอย่าง QBTS (FIX_C 1500, DIFF 50, LEAPS 100): BUY < 14.50, SELL < 15.00
- `ops.py release-plan --reference-price P` ให้ finding `leaps_value`: BLOCK เมื่อ P ≥ FIX_C/LEAPS (ไม่มี ack), WARN เมื่อ P ≥ (FIX_C−DIFF)/LEAPS, ต่ำกว่านั้น INFO พร้อมเป้าหุ้นจริงและขนาด BUY แรกของบัญชีที่ไม่มีหุ้นจริง
- ไม่ส่งราคา ⇒ WARN; runtime ไม่มีราคา ⇒ ack ที่ runtime ไม่เปลี่ยน; LEAPS=0 ⇒ ไม่มี finding นี้

## 2. ตาราง 17 คอลัมน์

- คอลัมน์และลำดับเดิมตาม `COLUMN_ORDER` (ไม่เพิ่ม/ลบ/สลับ) round 2dp เฉพาะตอนแสดง
- คอลัมน์ 7 "จำนวนถือครอง (หุ้น)" = `effective` (หุ้นจริง + LEAPS) ตรงกับ `Vₙ = holdings × Pₙ`
- ค่าแยกเขียนเป็น field แบนบนแถว (ไม่ใช่คอลัมน์) เฉพาะ chain ที่ใช้ LEAPS:
  `real_holdings`, `leaps_shares`, `leaps_shares_changed_from` (แถวที่ LEAPS เปลี่ยน),
  `post_execution_real_holdings` (หลัง fill), `funding_baseline_cancelled_by_leaps` (กรณีพิเศษข้อ 5)
- dashboard (`streamlit_lego_prod`) แสดงค่าเดียวกัน และชี้เมาส์ที่คอลัมน์ 7 เห็น
  "หุ้นจริง X + LEAPS Y = Z" พร้อม caption ใต้หัวตาราง

## 3. Guard และ `prev_holdings` (เก็บเป็นหุ้นจริงเสมอ)

`prev_holdings` ใน chain state = **หุ้นจริง** ทุกจุดที่เขียน (commit, finalize, paper fill) เพราะ

1. guard position หาย (`check_holdings_continuity` / `position_vanished` / `HoldingsAnomaly`) มีไว้จับ positions
   response ที่ทำ symbol หาย ซึ่งรายงานแต่หุ้นจริง ถ้าใช้ effective ตอนถือ LEAPS ค่าไม่มีวันเป็น 0 guard จะถูกปิดเงียบ
2. paper mode ใช้ `prev_holdings` เป็น paper book — ถ้าเป็น effective จะบวก LEAPS ซ้ำทุก slot
3. expiry proof, drift check, `validate_preview_funding` (SELL ≤ ที่ถือ) และ fractional gate เทียบกับ broker จริง
4. operator/audit เทียบ `webull_lego_state.prev_holdings` กับ position ที่ Webull ได้ตรงตัว

จุดที่ยังเป็นหุ้นจริงทั้งหมด: `snapshot.holdings`, intent `decision_holdings`, `run_id` (= `client_order_id` 32 ตัว
รูปแบบเดิม), drift check ตอน dispatch, Preview funding, fill confirmation

## 4. Fill: `holdings_after` = หุ้นจริง + LEAPS

- `ExecutionFill.from_broker(price, qty, real_after, leaps)`: `holdings_after = real_after + LEAPS ของ intent`
  (ตรง ledger คอลัมน์ 7) และเก็บ `real_holdings_after` ไว้คู่กัน LEAPS = 0 ได้ object เดิมทุกประการ
- ตรวจ fill (`position_vanished`, `_holdings_match_fill`) ด้วยหุ้นจริงก่อน/หลังเหมือนเดิม
- `state.prev_holdings = real_holdings_after`; row `post_execution_holdings` = effective
- `lego_admin_reconcile` และ `tools/trace_audit` (position walk) เดินบนหุ้นจริง

## 5. Rollover: เปลี่ยน `LEGO_LEAPS` กลาง chain

- `config_hash`/`chain_key` ไม่มี LEAPS → **chain เดิม** P₀/Aₙ/Eₙ ต่อเนื่อง ไม่ rebase
- ฐานใหม่มีผลตั้งแต่แถวถัดไป: แถวแรกที่ใช้ค่าใหม่มี `leaps_shares_changed_from`, state เก็บ `leaps_shares`,
  `webull_lego_warnings/leaps_shares_changed` และ flight recorder D07 บันทึก event
- intent ที่**ยังไม่ส่ง**ซึ่งตัดสินด้วย LEAPS เดิม → `SUPPRESSED_STATE_CHANGED`
  (`LEGO_LEAPS changed since decision`) ก่อน Preview; order ที่ส่งแล้ว reconcile/finalize ด้วย LEAPS ของ intent เอง
- chain ใหม่ที่เริ่มจาก 0 หุ้นได้ initial-funding baseline เฉพาะเมื่อ effective = 0; ถ้า LEAPS เข้ามาก่อน fill แรก
  baseline ถูกปิด (`funding_baseline_cancelled`) และบันทึกเป็น event — fill ถัดไปเป็น fill ปกติ
- **FIX_C** (ผู้ใช้เลือกคงเดิม): FIX_C อยู่ใน `chain_key` การเปลี่ยน FIX_C จึงเปิด chain ใหม่ chain เก่าไม่ถูกแก้
  (ไม่ใช่ rebase ในที่เดิม) — beta rollover ของ FIX_C ใน chain เดิมต้องแก้ recurrence และ chain key จึงแยกเป็น PR อื่น

## 6. Config และ release

- `config.py`: `LEGO_LEAPS` ไม่ตั้ง/ว่าง → 0; ต้องเป็นตัวเลข finite ≥ 0 (ปฏิเสธ `-1`, `nan`, `inf`, `abc`, `1,000`, `1e400`)
- `OperatorSettings.leaps_shares` เข้า `canonical()` **เฉพาะเมื่อ > 0** → ที่ 0 config hash / release binding / PROD ack เหมือนเดิม;
  ค่าอื่นต้อง plan release ใหม่ (script คำนวณให้เอง) เหมือนการเปลี่ยน FIX_C
- deploy: `LEGO_LEAPS_OVERRIDE` ใน `deploy/cloudshell-all-in-one.sh` (ไม่ระบุ = ใช้ค่าที่ deploy อยู่, ไม่มี = 0) และ
  `deploy/cloudshell-prod-observe.sh` (ไม่ระบุ = 0 ทุกครั้ง); `ops.py` `DEPLOY_OVERRIDES`; log `lego_tick_completed`
  มี `decision.leaps_shares/real_holdings/effective_holdings` เมื่อใช้ LEAPS

## 7. Deploy ด้วย Cloud Shell (แบบง่าย)

1. เปิด https://shell.cloud.google.com แล้วเลือก project `lego-firebase`
2. เอาโค้ดล่าสุด (หลัง PR merge):

   ```bash
   cd ~/lego && git fetch origin && git checkout main && git pull --ff-only
   ```

3. (แนะนำ) รันเทสต์ก่อน:

   ```bash
   python3 -m pip install -r requirements.txt pytest && python3 -m pytest -q
   ```

4. Deploy PROD **paper** พร้อม LEAPS (ตัวอย่าง UBER ~70 USD, LEAPS 20 หุ้นจำลอง ≈ 1,400 USD < FIX_C 3,000):

   ```bash
   LEGO_MODE_OVERRIDE=paper LEGO_FIX_C_OVERRIDE=3000 LEGO_DIFF_OVERRIDE=5 LEGO_LEAPS_OVERRIDE=20 bash deploy/cloudshell-prod-observe.sh
   ```

   script จะถามราคาอ้างอิง (`LEGO_REFERENCE_PRICE`) และการยืนยัน Preview (`LEGO_PREVIEW_CONFIRMED=yes`) ถ้ายังไม่ตั้ง
   ถือ LEAPS 1 สัญญา (`LEGO_LEAPS_OVERRIDE=100`) ต้องตั้ง FIX_C > 100 × ราคา (เช่น 10000) ไม่อย่างนั้นทุกแถวที่ต้องขายจะ PASS
5. ตรวจว่าค่าเข้าไปจริง:

   ```bash
   gcloud functions describe lego-tick-prod --gen2 --region=asia-southeast1 --project=lego-firebase --format='value(serviceConfig.environmentVariables.LEGO_LEAPS)'
   ```

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed"' --project=lego-firebase --freshness=1h --limit=10 --format='table(timestamp,jsonPayload.decision.status,jsonPayload.decision.real_holdings,jsonPayload.decision.leaps_shares,jsonPayload.decision.effective_holdings)'
   ```

6. เปลี่ยนค่า LEAPS: รันคำสั่งข้อ 4 ซ้ำด้วยค่าใหม่ — แถวถัดไปใช้ฐานใหม่และมี event บันทึก; กลับไปไม่ใช้ LEAPS: `LEGO_LEAPS_OVERRIDE=0`
7. หยุดชั่วคราว:

   ```bash
   gcloud scheduler jobs pause lego-tick-prod --location=asia-southeast1 --project=lego-firebase
   ```

dashboard (`streamlit_lego_prod`) บน Streamlit Community Cloud ดึง `main` ใหม่เองหลัง merge (Manage app → Reboot ถ้ายังไม่เห็น)

**เงินจริงพร้อม LEAPS** (audit 3 พ.ย.): ใช้ `deploy/cloudshell-prod-live.sh` (`docs/PROD_LIVE_RUNBOOK_TH.md` ข้อ 16).
ก่อน plan, account preflight (`tools/prod_live_preflight.py`, อ่านอย่างเดียว) ตรวจ:
- LEAPS ที่ถือจริง = long CALL × `option_contract_multiplier` ≥ `LEGO_LEAPS` และหมดอายุหลัง window
- ทุกแถว position ของ symbol มี `instrument_type` (แถวที่ไม่มี type จะถูก `_extract_qty` นับเป็นหุ้น)
- ราคาเทียบช่วงข้างบน
- หุ้นจริงที่ต้องถือก่อน เพื่อให้ order แรกพอดี caps canary

ตรวจไม่ผ่านข้อใด ⇒ BLOCK ก่อน deploy.

## 8. เทสต์

- `test_leaps_stock_replacement.py` (47 เทสต์): config, oracle LEAPS=0 เทียบโค้ดก่อนแก้ทุกบิต (900 กรณี),
  BUY/SELL/PASS, holdings=0 + LEAPS, fractional policy, guard บนหุ้นจริง, fill จริงและ paper fill, admin reconcile,
  trace_audit parity (4,320 กรณี), session paper ทั้งวัน, เปลี่ยน LEAPS กลาง chain, intent เก่าถูกหยุด, log, wiring ของ tick
- เทสต์เดิม: เพิ่ม `LEGO_LEAPS` ใน `SCRIPT_BINDING_NAMES` ของ `test_release_plan.py` ให้ตรง script (ไม่ลด/ไม่ skip)
- mutation 14/14 ถูกจับ (lego) และ 5/5 (dashboard)

## 9. สมมติฐานและข้อจำกัด

- `LEGO_LEAPS` เป็นค่าที่ operator ใส่เอง (delta-adjusted ได้ เช่น 85.5) ไม่ได้อ่านจาก broker และไม่ตาม delta/วันหมดอายุอัตโนมัติ
- order ของ LEAPS ที่เปิดค้างใน symbol เดียวกันทำให้ dispatch หยุดเป็น `SUPPRESSED_ACTIVE_ORDER` (fail closed ตามเดิม)
- caps ของ release (`LEGO_MAX_ORDER_*`) ไม่เปลี่ยน: LEAPS ทำให้ BUY เล็กลงเท่านั้น
- `LEARNING_GUIDE_TH.html` ไม่แก้ (ถูกล็อก hash ใน characterization test)
