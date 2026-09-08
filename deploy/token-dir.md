# `WEBULL_TOKEN_DIR` — ทำให้ preflight `token_ready` ผ่านได้

เอกสารนี้แก้อาการเดียว: **แถวเป็น `READY_BUY`/`READY_SELL` ทุก slot แต่จำนวนถือครองไม่ขยับ**

## ต้นเหตุ

Cloud Functions ให้ container เขียนได้ที่ `/tmp` เท่านั้น ดังนั้น `WEBULL_TOKEN_DIR` ที่ไม่ได้ตั้ง
(default `/tmp/webull_token`) ทำให้ `token_dir_is_ephemeral()` เป็น `True` **ทุก deployment**

`token_health()` รายงานสองเรื่องแยกกัน:

| key | ตอบคำถาม | ถูกกระทบจาก dir ที่ไม่คงอยู่ |
|---|---|---|
| `ok` | "มีอะไรที่ต้องบอก operator ไหม" | ใช่ — เป็นที่มาของ `token_warning` |
| `ready` | "token นี้เซ็น request ได้ตอนนี้ไหม" | เฉพาะเมื่อยังไม่ยอมรับความเสี่ยง |

preflight `token_ready` อ่าน **`ready`** — หรือรับหลักฐานว่า token นี้เพิ่งเซ็น request
สำเร็จใน invocation เดียวกัน มาลบล้างเหตุผลที่ได้มาจากการส่องไฟล์ในเครื่อง
(`live_proof_supersedable`) ดูหัวข้อถัดไป

## ⚠️ ถ้า broker ปิด token check ไว้ ไฟล์นี้จะไม่มีวันเกิด

ก่อนจะไล่ตาม volume ให้ดู log ของ SDK หนึ่งบรรทัดนี้ก่อน:

```
webull.core.http.initializer.client_initializer INFO _check_token_enable result is False
```

`ClientInitializer.init_token()` ถาม broker ว่าเปิด token check ไหม ถ้าตอบ `False`
มัน **return ก่อนสร้าง `TokenManager`** — ไม่มีอะไรเขียน `token.txt` และจะไม่มีวันมี
SDK เซ็นทุก request ด้วย HMAC อย่างเดียว ซึ่งใช้ได้จริง (`get_account_position` ผ่าน)

นี่คือสถานะของ UAT app ที่ chain นี้รันอยู่ และเป็นต้นเหตุของเคส 2026-07-29/30:
37 แถวติดกัน, 33 แถวเป็น `READY_*`, `จำนวนถือครอง (หุ้น)` ค้างที่ `8.78392` ทุกแถว เพราะ
`token_health()` อ่าน "ไม่พบไฟล์" เป็น "ไม่มีอะไรให้เซ็น"

**ในกรณีนี้ทั้งสองทางเลือกข้างล่างไม่ได้แก้อะไร** (ไฟล์ไม่เกิดอยู่ดี) — สิ่งที่แก้คือ
preflight ยอมรับหลักฐานการเซ็นจริงแทนการส่องไฟล์ ซึ่งอยู่ในโค้ดแล้ว ทางเลือกข้างล่าง
ยังคุ้มค่าเมื่อ broker **เปิด** token check เท่านั้น

## ทางเลือก 1 (แนะนำ) — mount volume ที่คงอยู่

```bash
gcloud storage buckets create gs://lego-firebase-webull-token \
    --location=asia-southeast1 --uniform-bucket-level-access
```

แล้วเพิ่มเข้า service ทั้งสามตัว (`lego-one-row`, `lego-order-worker`, `lego-archive-worker`):

```yaml
spec:
  template:
    metadata:
      annotations:
        # GCS FUSE ต้องใช้ execution environment gen2
        run.googleapis.com/execution-environment: gen2
    spec:
      volumes:
      - name: webull-token
        csi:
          driver: gcsfuse.run.googleapis.com
          volumeAttributes:
            bucketName: lego-firebase-webull-token
      containers:
      - name: worker
        volumeMounts:
        - name: webull-token
          mountPath: /mnt/webull-token
        env:
        - name: WEBULL_TOKEN_DIR
          value: /mnt/webull-token
```

service account ต้องมี `roles/storage.objectAdmin` บน bucket นี้:

```bash
gcloud storage buckets add-iam-policy-binding gs://lego-firebase-webull-token \
    --member=serviceAccount:372581079992-compute@developer.gserviceaccount.com \
    --role=roles/storage.objectAdmin
```

ผลที่ได้ครบทุกอย่าง: `ok` และ `ready` เป็น `True` พร้อมกัน, `token_warning` หายไป, และ
`ensure_token_fresh` เริ่มต่ออายุ token ให้จริง (บน `/tmp` มันข้ามการ refresh เสมอ เพราะการหมุน
token จะทิ้ง container อื่นค้าง)

## ทางเลือก 2 — ยอมรับ `/tmp` ชั่วคราว

ใช้เมื่อยัง mount ไม่ได้และต้องการให้ order เดินวันนี้:

```yaml
        - name: LEGO_ALLOW_EPHEMERAL_TOKEN_DIR
          value: 'true'
```

สิ่งที่ยอมรับไปด้วย:

- token หายเมื่อ instance ถูกรีไซเคิล → ต้องมีคนกด 2FA ในแอปใหม่ ไม่งั้นได้ `ERROR_INIT_TOKEN`
- `ensure_token_fresh` **ยังข้ามการ refresh** → token ตายเมื่อครบ 15 วันแน่นอน
- `token_warning` และ `webull_lego_warnings/webull_token` ยังขึ้นทุก slot (ตั้งใจให้ขึ้น)

flag นี้ให้อภัยเฉพาะข้อ "dir ไม่คงอยู่" — `status` ≠ `NORMAL` หรือเหลืออายุน้อยกว่า
`LEGO_TOKEN_REFRESH_MARGIN_DAYS` ยังบล็อกเหมือนเดิม ส่วน "ไม่พบ token file" ตอนนี้
preflight ผ่านได้เมื่อมีหลักฐานว่า token เพิ่งเซ็น request สำเร็จ (ดูหัวข้อ token check
ข้างบน) แต่ flag นี้ไม่เกี่ยว — มันไม่ได้ยกข้อนั้นให้

## ยืนยันว่าแก้แล้ว

```bash
curl -s -X POST "$LEGO_ONE_ROW_URL" -H "Authorization: Bearer $(gcloud auth print-identity-token)" | jq
```

- ไม่มี key `outbox_blocked` / `outbox_blocked_checks` = preflight ผ่านครบ
- `webull_lego_order_outbox/{chain_key}` มี intent ใหม่ status `PENDING_DISPATCH`
- รอบถัดไปของ `lego-order-worker` ใช้เวลามากกว่า ~1 วินาที (มี broker call จริง)
  แทนที่จะเป็น ~0.2 วินาทีของ queue ว่าง

