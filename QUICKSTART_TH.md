# Quickstart และ Operations — LEGO PRINCIPAL v2

## 1. ตรวจ candidate แบบ local

ใช้ Python 3.12 และติดตั้ง dependency ที่ pin:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

ตั้ง 3 ค่าบังคับและ deployment identity สำหรับ `check`:

```powershell
$env:LEGO_SYMBOL='AAPL'
$env:LEGO_FIX_C='1500'
$env:LEGO_DIFF='25'
$env:LEGO_DNA_BUNDLE='strategy.example.json'
$env:LEGO_MODE='observe'
$env:LEGO_ACTIVE='false'
$env:WEBULL_ENV='UAT'
$env:WEBULL_ACCOUNT_ID='YOUR-UAT-ACCOUNT'
$env:FIREBASE_DB_URL='https://YOUR-UAT-DB.firebasedatabase.app'
$env:LEGO_CANDIDATE_HASH='OUTPUT-FROM-RELEASE-MANIFEST'
python ops.py check
```

`strategy.example.json` เป็น bypass สำหรับ chain ใหม่ ไม่ใช่ metadata ที่นำไปเติม
DNA trained เดิม หาก DNA เดิมไม่มี interval/origin/calendar fingerprint ให้ block
activation จนกว่าจะหา metadata จริงได้

## 2. Secret Manager และ token bootstrap

สร้าง secret แยก UAT/PROD: app key, app secret, account ID และ token. Runtime SA
ต้องมี `secretAccessor`; admin ที่ bootstrap token จึงมี `secretVersionAdder`.

token file เป็น format ของ SDK: `token`, `expires`, `NORMAL` อย่างละหนึ่งบรรทัด.
ตรวจ dry-run ก่อน แล้วค่อยเพิ่ม versionเมื่อได้รับอนุญาต/2FA แล้ว:

```powershell
python ops.py bootstrap-auth --token-file C:\secure\token.txt `
  --secret projects/PROJECT/secrets/webull-token-uat
python ops.py bootstrap-auth --token-file C:\secure\token.txt `
  --secret projects/PROJECT/secrets/webull-token-uat --apply
```

ห้าม commit token หรือแสดง payload ใน log. Runtime hydrate secret ลง `/tmp` ตอน
cold start แต่ไม่ rotate token เอง; rotation เป็น admin operation แบบ serialized.

ตรวจคำสั่งสร้าง service accounts/secrets/IAM แบบ dry-run ก่อน (ไม่แตะ cloud):

```powershell
.\deploy\bootstrap-cloud.ps1 -Environment UAT -ProjectId PROJECT-UAT
```

เพิ่ม `-Apply` เฉพาะเมื่อ project และ gcloud identity ถูกตรวจแล้ว; script ไม่รับหรือ
พิมพ์ secret payload และรันซ้ำได้โดยไม่สร้าง account/secret container ซ้ำ.

## 3. Deploy observe จาก build เดียวกัน

script เป็นตัวเตรียม deployment ที่ตรวจทานได้ ไม่ใช่ authorization ให้ deploy:

```powershell
.\deploy\bootstrap-cloud.ps1 -Environment UAT -ProjectId PROJECT-UAT
# ตรวจ dry-run ก่อน; เพิ่ม -Apply เฉพาะเมื่ออนุญาตสร้าง IAM/secrets แล้ว
```

```powershell
.\deploy\deploy.ps1 -Environment UAT -ProjectId PROJECT-UAT `
  -DatabaseUrl https://UAT-DB.firebasedatabase.app `
  -CandidateHash CANDIDATE -Symbol AAPL -PrincipalUsd 1500 -DiffUsd 25 `
  -DnaBundle strategy.example.json

.\deploy\deploy.ps1 -Environment PROD -ProjectId PROJECT-PROD `
  -DatabaseUrl https://PROD-DB.firebasedatabase.app `
  -CandidateHash CANDIDATE -Symbol AAPL -PrincipalUsd 1500 -DiffUsd 25 `
  -DnaBundle strategy.example.json
```

ทั้งสองคำสั่ง default `mode=observe, active=false`; function คือ `lego-tick-uat`
และ `lego-tick-prod`, entrypoint `lego_tick`, timeout 45s, concurrency/max instances 1.
ต้อง inventory cloud ยืนยันว่า legacy decision/worker/archive ถูกตัด traffic แล้วจึงลบ
เมื่อได้รับอนุญาต และสุดท้ายมี functions รวมไม่เกิน 2.

## 4. Scheduler/IAM

สร้าง Scheduler SA แยก environment ให้ invoke ได้เฉพาะ function ของตัวเอง และตั้ง
OIDC audience เท่ากับ URI จริง:

```powershell
$url = gcloud functions describe lego-tick-uat --gen2 `
  --region asia-southeast1 --project PROJECT-UAT --format='value(serviceConfig.uri)'
gcloud scheduler jobs create http lego-tick-uat --location asia-southeast1 `
  --schedule='* * * * *' --time-zone='UTC' --uri=$url --http-method=POST `
  --oidc-service-account-email=lego-scheduler-uat@PROJECT-UAT.iam.gserviceaccount.com `
  --oidc-token-audience=$url --max-retry-attempts=0 --project PROJECT-UAT
```

ทำซ้ำสำหรับ PROD ด้วย project/SA/URL คนละชุด RTDB rules ใช้ `firebase deploy
--only database`; client write ปิดทุก money path และ Admin SDK เขียนผ่าน runtime SA.
หลัง deploy ใช้ `configure-scheduler.ps1` แบบ dry-run แล้วค่อย `-Apply` เพื่อผูก
invoker และ OIDC audience กับ URI ที่อ่านจาก revision จริง:

```powershell
.\deploy\configure-scheduler.ps1 -Environment UAT -ProjectId PROJECT-UAT
```
ใช้ `.\deploy\configure-scheduler.ps1` แบบ dry-run ก่อน แล้วเพิ่ม `-Apply` หลัง
observe deployment สำเร็จ; script ผูก OIDC audience กับ URI ที่อ่านจาก revision จริง.

## 5. Observe → activate

ตรวจ read-only UAT account/instrument/balance/positions/snapshot/open orders และ
exact-payload Preview ก่อน Place. `python ops.py status` อ่าน chain status โดยไม่ส่ง
order. หากผ่านและผู้ใช้อนุญาตขอบเขตการทดสอบ ให้สร้าง release binding:

```powershell
python ops.py release-binding
```

นำค่าที่ได้ไป deploy revision เดิมด้วย `-Mode trade -Active $true
-ReleaseAuthorization VALUE`. Binding เปลี่ยนตาม candidate, environment และ account;
นำของ UAT ไปเปิด PROD ไม่ได้

การทดสอบ UAT lifecycle ต้องระบุ account alias, symbol, side, quantity และวงเงินสูงสุด,
แตะเฉพาะ order ID ของตัวเอง และต้องได้ nonzero fill จริงจึงผ่าน A19. Shared test account
อาจถูกรบกวน; ผลนั้นเป็น BLOCKED/INCONCLUSIVE ไม่ใช่ PASS.

Production เริ่ม observe และพิสูจน์ zero submissions ก่อน Production canary ต้องขอ
authorization ใหม่ที่ระบุขอบเขตชัดเจน หากไม่มีให้ A21 เป็น BLOCKED.

## 6. Pause, reconcile, rollback

- Pause: deploy `mode=observe` หรือ `active=false`; Scheduler ยังทำงานเพื่อ recovery.
- อย่าลบ fence เพราะ timeout/retry exhaustion. ตรวจ exact client ID กับ detail/open/
  history และ broker position ก่อน operator resolution.
- Cutover: disable intent ใหม่ของระบบเก่า → reconcile submitted/unknown จนหมด → export
  + checksum → เริ่ม v2 genesis → เปิด writer ใหม่เพียงตัวเดียว.
- ถ้ามี realized `open_legs` schema เดิม ให้ dry-run
  `python tools/migrate_realized_fifo_v3.py CHAIN_KEY` แล้วใช้ `--apply` ทีละ batch
  จน `complete=true`; checkpoint resume ได้และ hot head ใหม่เก็บ projection ไม่เกิน
  16 lots. ก่อน finalize เท่านั้นจึงยกเลิก migration epoch (เก็บ pages ไว้) ได้ด้วย
  `--rollback --confirm`.
- หลัง export RTDB เป็น JSON ให้รัน `python tools\migration_audit.py BACKUP.json`;
  `cutover_safe` ต้องเป็น true และเก็บ `export_sha256` ไว้ใน evidence ก่อนเปิด v2.
- Rollback: disable intent ใหม่ก่อน, reconcile order ปัจจุบัน, แล้ว restore code ที่อ่าน
  schema v2 ได้ ห้าม restore RTDB snapshot ทับ execution ที่ broker ทำไปแล้ว.

หลัง export RTDB เป็น JSON ให้ audit แบบ read-only; `cutover_safe=false` ต้องหยุดทันที:

```powershell
python tools\migration_audit.py C:\secure\rtdb-backup.json
```

## 7. Definition of done

local tests และ mock benchmark ไม่ทำให้ cloud/live gates ผ่านเอง `ACCEPTANCE.json`
ต้องผูกทุกผลกับ candidate hash, dependency lock hash, environment และ timestamp.
credential/ตลาดปิด/ไม่มี authorization เป็น BLOCKED เสมอ ไม่ลด denominator.
