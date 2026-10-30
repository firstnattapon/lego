# Audit: LEGO เทียบ Webull OpenAPI (TH) ทั้งชุด — 30 ตุลาคม 2026

## สรุป

- endpoint ทั้ง 20 ตัวที่ LEGO เรียก ตรงกับเอกสารทางการ, SDK 3.0.1 และคำตอบจริงบน PROD ทั้ง method, path และ field
- พบ bug 2 ข้อ แก้แล้ว
  1. map route ของ Order History ผิด ทำให้ trace และ error ของทางสำรองตอน reconcile ไม่ถูกจัดประเภท
  2. ระบบตัดสินวันหมดอายุ token จากสำเนาที่เก็บใน secret ตอนออก token แทนคำตอบปัจจุบันของ Webull
- เอกสารทางการเรียกฟิลด์วันหมดอายุว่า `expires_at` ส่วน SDK และ PROD ใช้ `expires` ตอนนี้โค้ดรับได้ทั้งสองชื่อ
- แก้ข้อความ "token ตายที่ 15 วัน" ในโค้ดและเอกสารที่ใช้งานอยู่ (ส่วน audit เก่าที่มีวันที่กำกับเก็บไว้ตามเดิม)
- ไม่ต้องอัปเกรด SDK และไม่ต้องเรียก endpoint เพิ่ม

## แหล่งอ้างอิง

| แหล่ง | ใช้อย่างไร | ความน่าเชื่อถือ |
|---|---|---|
| เอกสารทางการ developer.webull.co.th (ดึง 30 ต.ค.: `llms.txt` และหน้า `.md` 20 หน้า ซึ่งมี OpenAPI JSON ครบ request/response) | แหล่งหลักของ field, format, enum, rate limit | สูง |
| คำตอบจริงของ Webull บน PROD (export 29 ต.ค. ทุก route ตอบ 200) | ตัดสินเมื่อเอกสารขัดกับของจริง | สูงสุด |
| SDK ทางการ 3.0.1 (ตรวจ sha256 จาก PyPI) | route, x-version, ลำดับการเรียกตอนสร้าง client | สูง |
| SDK 3.0.4 (17 ต.ค.) | diff กับ 3.0.1: เพิ่มแค่ news, community, insider/institution, transfer activities; ส่วนที่ LEGO ใช้ไม่เปลี่ยน | สูง |
| `webull-llms.md` (ผู้ใช้อัปโหลด) | ประกอบจากหน้าเว็บทางการ, path ตรง 20/20, บอกเองว่า schema ไม่ครบ | สูงในส่วนที่มี |
| `webull_openapi_documentation__reference.html` (ผู้ใช้อัปโหลด) | ใช้ได้เฉพาะส่วน guide และ changelog | ส่วน API Reference ผิด (ดูหัวข้อท้าย) |

## Endpoint ในดัชนีทั้งหมด

| Endpoint | Route | LEGO ใช้ที่ | PROD |
|---|---|---|---|
| Create / Check Token | `POST /auth/tokens/create`, `POST /auth/tokens/check` | SDK ตอนสร้าง client, `verify_production_token`, `ops.py token-status` | 200 |
| Account List | `GET /trading/accounts/list` | `verify_account_access` | 200 |
| Account Balance | `GET /trading/assets/balances/get` | `fetch_buying_power` | 200 |
| Account Positions | `GET /trading/assets/positions/list` | `fetch_holdings` | 200 |
| Get Stock Instrument | `GET /trading/instruments/stocks/profiles/list` | `fetch_instrument_capability` | 200 |
| Snapshot | `GET /market-data/stocks/snapshots/list` | `fetch_snapshot` | 200 |
| Preview Order | `POST /trading/orders/preview` | `preview_market_order_result` | 200 |
| Open Orders | `GET /trading/orders/open-orders/list` | `fetch_open_orders` | 200 |
| Place Order | `POST /trading/orders/place` | `place_market_order` (ส่งครั้งเดียว, ล็อกใน preview/paper) | ยังไม่เคย (UAT ผ่าน) |
| Order Detail | `GET /trading/orders/get` | `fetch_order_detail` | ยังไม่เคย (UAT ผ่าน) |
| Cancel Order | `POST /trading/orders/cancel` | `cancel_order` | ยังไม่เคย (UAT ผ่าน) |
| Order History | `GET /trading/orders/historical-orders/list` | `find_recent_order_by_client_id` (ทางสำรองตอน reconcile), smoke | ยังไม่เคย |
| Replace, Tick, Quotes, Footprint, Historical Bars ×2, MQTT, Trade Events (gRPC) | – | ไม่ใช้: กลยุทธ์ใช้แค่ราคาล่าสุดต่อ slot, Cloud Function ถือ connection ไม่ได้, ถาม Order Detail แทน event | – |

## ตรวจกับเอกสารทางการ

| เรื่อง | เอกสารทางการ | LEGO |
|---|---|---|
| วันหมดอายุของ token | Create/Check Token ตอบ `token`, `expires_at` (Unix ms; หลังเวลานี้ token ใช้ไม่ได้), `status` (PENDING/NORMAL/INVALID/EXPIRED); guide: `INVALID` เมื่อไม่เรียก API 15 วันติดกัน; ไม่มี refresh API | ใช้คำตอบของ Check Token (ชื่อ `expires` หรือ `expires_at`) ไม่ใช่สำเนาใน secret |
| เวลาใน Order History | `start_time`/`end_time` รูปแบบ `yyyy-MM-dd'T'HH:mm:ss.SSS'Z'`; ค่าเริ่มต้น 7 วัน, ย้อนได้ 6 เดือน; `pagination_key` | ส่ง `…:56.789Z` ตรงรูปแบบ; จำกัดการกู้คืนที่ 7 วัน |
| status ของ order | PENDING, SUBMITTED, CANCELLED, FILLED, FAILED, PARTIAL_FILLED | `lego_orders.TERMINAL_STATUSES` ครอบ FILLED/CANCELLED/FAILED; ที่เหลือรอต่อ |
| body ของ Preview/Place | required: `client_order_id`, `combo_type`, `entrust_type` (QTY/AMOUNT), `instrument_type`, `market`, `order_type`, `side`, `symbol`, `time_in_force`; `support_trading_session` มี CORE | `build_order_payload` ส่ง MARKET/DAY/QTY/CORE ครบ |
| `client_order_id` | ≤ 32 ตัว, ห้ามซ้ำในบัญชี, A–Z a–z 0–9 `-` `_` | run_id hex 32 ตัว; `cancel_order` ตรวจ regex เดียวกัน |
| `position_intent` | ใช้กับบัญชี margin เท่านั้น; Account List: `account_type` = `CASH` อย่างเดียว | ไม่ต้องส่ง |
| คำตอบ Preview / Place / Cancel | Preview: `estimated_cost`, `estimated_transaction_fee`; Place/Cancel: `client_order_id`, `order_id` | `preview_market_order_result`, `validate_place_response` |
| fee ใน Order Detail | `orders[].commission.actual_commission` + `orders[].fees[].actual_value` | `lego_orders._actual_fee` รวมสองส่วนนี้; ขาดส่วนใดถือว่าไม่รู้ |
| Balance / Positions | `account_currency_assets[].buying_power`; positions เป็น array ไม่มี pagination | `parse_buying_power`, `_extract_qty` |
| Instrument / ETF | `category` มีแค่ `US_STOCK`; ETF คือ `sub_category = ETF`; status OC/CO/NT; มี `fractionable` | ด่าน instrument ไม่บล็อก ETF; snapshot ใช้ `US_ETF` ได้ผ่าน `LEGO_MARKET_CATEGORY` |
| Rate limit | instruments 60/60s, account 10/30s, order 15/s, order query 40/2s, market data 300/60s | การเรียกต่อนาทีต่ำกว่าทุก limit; comment ใน `webull_io.py` แก้ตามนี้ |
| `place_time` / `filled_time` | ยังอยู่ใน reference TH (changelog global บอก deprecated) | ใช้แค่ข้อมูลวินิจฉัยใน trace |
| ความไม่สอดคล้องในเอกสารเอง | หน้า Check Token ระบุ header `x-app-secret` และ HMAC-SHA1 เป็นค่าเริ่มต้น ขัดกับหน้า Signature (ไม่ส่ง secret, HMAC-SHA256) | ยึด SDK และ PROD |

## ข้อค้นพบและการแก้

| # | ข้อค้นพบ | การแก้ |
|---|---|---|
| 1 | cold start ปฏิเสธ token เมื่อสำเนาวันหมดอายุใน secret ผ่านไปแล้ว ก่อนถาม Webull; wrapper ก็สั่ง 2FA ตามสำเนา | `2288a82`, `7522db7`: PROD ถาม Check Token ก่อนสร้าง client ของ SDK (ซึ่งเรียก create_token) แล้วใช้วันหมดอายุของ Webull; wrapper ถาม `ops.py token-status` ก่อน 2FA |
| 2 | map route ของ Order History เป็น `/trading/orders/history/list` จึงถูกนับเป็น `sdk_request` (trace เก็บ order ของ symbol อื่นทั้งหน้า, error ไม่บอก operation) | `da3610b`: `_OPERATION_BY_ROUTE` และเทสต์ที่เรียก SDK จริงทุก route |
| 3 | Order History ไม่เคยถูกเรียกกับ Webull จริง | `da3610b`: smoke อ่าน history 24 ชม. (`checks.order_history_read`) |
| 4 | เอกสารเรียกฟิลด์ `expires_at`, SDK ใช้ `expires` | `7522db7`: รับได้ทั้งสองชื่อ ไม่มีทั้งคู่ = ปฏิเสธ |
| 5 | ข้อความ "token ตายที่ 15 วัน" | แก้ใน `webull_io.py`, `decision_service.py`, wrapper, `PROD_LIVE_RUNBOOK_TH.md` หัวข้อ 7, `PROD_OBSERVE_TH.md`, `deploy/token-dir.md` |
| 6 | `INVALID_TOKEN` อาจหมายถึง signature ไม่ตรง (Trading API FAQ) | หมายเหตุใน runbook หัวข้อ 7 |
| 7 | comment rate limit บอก market data 60/60s | แก้เป็นตัวเลขตามเอกสาร |

ลำดับที่รักษาไว้: Check Token (อ่านอย่างเดียว ไม่ส่ง SMS) รันก่อน `TradeClient`/`DataClient` เสมอ ถ้า Webull ตอบว่า token ใช้ไม่ได้ ระบบหยุดก่อน create_token และเปิด auth circuit จึงไม่มี 2FA จาก scheduler; UAT ไม่เปลี่ยน; runtime ไม่เขียน secret

## ไฟล์ HTML ที่อัปโหลด: ส่วน API Reference ผิด

- path ขัดกับ PROD, SDK และเอกสารทางการ เช่น Check Token `GET /trading/v3/auth/token/status` (จริง `POST /auth/tokens/check`), Snapshot `/openapi/market-data/stock/snapshot` (จริง `/market-data/stocks/snapshots/list`), Balance `/trading/accounts/{id}/balance` (จริง `/trading/assets/balances/get`)
- schema ขัด: Check Token ไม่มีวันหมดอายุ (SDK `token_manager.py:136,173` ล้มถ้าไม่มี), balance เป็น field แบน, instrument ไม่มี `category`/`fractionable`
- order type `STOP`/`STOP_LIMIT` ขณะที่เอกสารทางการใช้ `STOP_LOSS`/`STOP_LOSS_LIMIT`; `entrust_type` `CASH` ขณะที่ทางการคือ `AMOUNT`
- มีร่องรอยว่าเขียนขึ้นใหม่: เวลาตัวอย่าง `2026-10-30T04:50:00Z` (วันที่สร้างไฟล์), token ตัวอย่าง `tk_a1b2…`

## ยังเปิดอยู่

- **`expires_at` เลื่อนตามการใช้หรือไม่**: เอกสารไม่บอก ถ้าเลื่อน ระบบเดินต่อโดยไม่ต้อง 2FA ใหม่; ถ้าไม่เลื่อน ต้อง 2FA ก่อนวันนั้นเหมือนเดิม วัดได้จาก log ของ SDK (ไม่พิมพ์ token):

  ```bash
  gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND textPayload:"create_token result"' --project=lego-firebase --freshness=3d --limit=500 --format='value(timestamp,textPayload)' | python3 -c 'import re, sys, datetime as d; rows = []; [rows.append((l.split("\t")[0], d.datetime.fromtimestamp((lambda e: e/1000 if e > 1e11 else e)(int(m.group(1))), d.timezone.utc))) for l in sys.stdin for m in [re.search(r"expires:(\d{10,13})", l)] if m]; rows.sort(); print(len(rows), "lines;", len({r[1] for r in rows}), "distinct expiry values"); [print("logged", r[0], "-> expires", r[1].isoformat()) for r in (rows[:1] + rows[-1:] if len(rows) > 1 else rows)]'
  ```

  ค่า expires ทุกบรรทัดเท่ากัน = ตายตัว; ใหม่ขึ้นตามเวลา log = เลื่อนตามการใช้
- **Place / Order Detail / Cancel บน PROD**: พิสูจน์ได้ด้วย canary เท่านั้น
- **Order History กับ server จริง**: รัน smoke (`AUTO_SUBMIT=false python lego_broker_smoke.py`) บน UAT แล้วดู `order_history_read: true`
