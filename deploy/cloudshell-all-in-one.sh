#!/usr/bin/env bash

set -Eeuo pipefail

# LEGO PRINCIPAL v2 — Google Cloud Shell all-in-one UAT deploy
#
# Safe defaults:
#   environment = UAT
#   mode        = observe
#   active      = false
#   order submission stays blocked until a candidate hash and release binding
#   for this exact UAT account are supplied explicitly.
#
# Run from the repository root:
#   bash deploy/cloudshell-all-in-one.sh
#
# Explicitly authorized UAT order submission:
#   EXPECTED_CANDIDATE_HASH=<64-hex> \
#   LEGO_RELEASE_AUTHORIZATION_OVERRIDE=<64-hex> \
#   LEGO_MODE_OVERRIDE=trade \
#   LEGO_ACTIVE_OVERRIDE=true \
#   bash deploy/cloudshell-all-in-one.sh

readonly PROJECT_ID="lego-firebase"
readonly REGION="asia-southeast1"
readonly ENVIRONMENT="UAT"
readonly SUFFIX="uat"
readonly FUNCTION_NAME="lego-tick-${SUFFIX}"
readonly SCHEDULER_JOB="lego-tick-${SUFFIX}"
readonly RUNTIME_SA_NAME="lego-runtime-${SUFFIX}"
readonly SCHEDULER_SA_NAME="lego-scheduler-${SUFFIX}"
readonly RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
readonly SCHEDULER_SA="${SCHEDULER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# --- OPERATOR SETTINGS & OVERRIDES ---
readonly MODE="${LEGO_MODE_OVERRIDE:-observe}"
readonly ACTIVE="${LEGO_ACTIVE_OVERRIDE:-false}"

readonly DATABASE_URL_OVERRIDE="${DATABASE_URL_OVERRIDE:-}"
readonly EXPECTED_CANDIDATE_HASH="${EXPECTED_CANDIDATE_HASH:-}"
readonly RELEASE_AUTHORIZATION_OVERRIDE="${LEGO_RELEASE_AUTHORIZATION_OVERRIDE:-}"
readonly TOKEN_SECRET_RESOURCE_OVERRIDE="${WEBULL_TOKEN_SECRET_OVERRIDE:-}"
readonly SMOKE_TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-120}"
readonly LEGO_SYMBOL_OVERRIDE="${LEGO_SYMBOL_OVERRIDE:-AAPL}"
readonly LEGO_FIX_C_OVERRIDE="${LEGO_FIX_C_OVERRIDE:-3000}"
readonly LEGO_DIFF_OVERRIDE="${LEGO_DIFF_OVERRIDE:-25}"
readonly LEGO_DNA_BUNDLE_OVERRIDE="${LEGO_DNA_BUNDLE_OVERRIDE:-strategy.example.json}"
readonly LEGO_SCHEDULE_OVERRIDE="${LEGO_SCHEDULE_OVERRIDE:-}"

step() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    echo >&2
    echo "DEPLOY FAILED at line ${BASH_LINENO[0]} (exit ${exit_code})." >&2
    exit "${exit_code}"
}

trap on_error ERR

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "ไม่พบคำสั่ง '$1'"
}

reject_comma() {
    local name="$1"
    local value="$2"
    [[ "${value}" != *,* ]] || fail "${name} ห้ามมี comma (,)"
}

require_sha256() {
    local name="$1"
    local value="$2"
    [[ "${value}" =~ ^[0-9a-f]{64}$ ]] || \
        fail "${name} ต้องเป็น SHA-256 ตัวพิมพ์เล็ก 64 ตัว"
}

require_boolean() {
    local name="$1"
    local value="$2"
    [[ "${value}" == "true" || "${value}" == "false" ]] || \
        fail "${name} ต้องเป็น true หรือ false"
}

ensure_service_account() {
    local name="$1"
    local display_name="$2"
    local email="${name}@${PROJECT_ID}.iam.gserviceaccount.com"

    if gcloud iam service-accounts describe "${email}" \
        --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "OK service account: ${email}"
    else
        echo "CREATE service account: ${email}"
        gcloud iam service-accounts create "${name}" \
            --display-name="${display_name}" \
            --project="${PROJECT_ID}"
    fi
}

ensure_secret_container() {
    local name="$1"

    if gcloud secrets describe "${name}" \
        --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "OK secret container: ${name}"
    else
        echo "CREATE secret container: ${name}"
        gcloud secrets create "${name}" \
            --replication-policy=automatic \
            --project="${PROJECT_ID}"
    fi
}

require_enabled_secret_version() {
    local name="$1"
    local state

    state="$(
        gcloud secrets versions describe latest \
            --secret="${name}" \
            --project="${PROJECT_ID}" \
            --format='value(state)' 2>/dev/null || true
    )"

    if [[ "${state}" != "ENABLED" ]]; then
        echo
        echo "Secret '${name}' ยังไม่มี latest version ที่ ENABLED"
        echo "เพิ่มค่าผ่าน Secret Manager หรือ secure administrator workflow แล้วรันใหม่"
        echo
        echo "ตัวอย่างคำสั่ง:"
        echo "  read -rsp 'Secret value: ' VALUE; echo"
        echo "  printf '%s' \"\${VALUE}\" | gcloud secrets versions add '${name}' --data-file=- --project='${PROJECT_ID}'"
        echo "  unset VALUE"
        exit 1
    fi

    echo "OK secret version: ${name}:latest"
}

read_existing_env() {
    local key="$1"

    [[ -n "${EXISTING_FUNCTION_JSON}" ]] || return 0

    printf '%s' "${EXISTING_FUNCTION_JSON}" | python3 -c '
import json
import sys

key = sys.argv[1]
try:
    data = json.load(sys.stdin)
    print(data.get("serviceConfig", {}).get("environmentVariables", {}).get(key, ""))
except Exception:
    print("")
' "${key}"
}

resolve_value() {
    local override="$1"
    local existing="$2"
    local fallback="$3"

    if [[ -n "${override}" ]]; then
        printf '%s' "${override}"
    elif [[ -n "${existing}" ]]; then
        printf '%s' "${existing}"
    else
        printf '%s' "${fallback}"
    fi
}

# -----------------------------------------------------------------------------
# 0. LOCAL PRE-FLIGHT
# -----------------------------------------------------------------------------

step "0/10 LOCAL PRE-FLIGHT"

require_command gcloud
require_command python3
require_command git

[[ "${MODE}" == "observe" || "${MODE}" == "trade" ]] || \
    fail "LEGO_MODE_OVERRIDE ต้องเป็น observe หรือ trade"
require_boolean "LEGO_ACTIVE_OVERRIDE" "${ACTIVE}"
[[ "${SMOKE_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || \
    fail "SMOKE_TIMEOUT_SECONDS ต้องเป็นจำนวนเต็มบวก"
(( SMOKE_TIMEOUT_SECONDS >= 30 )) || \
    fail "SMOKE_TIMEOUT_SECONDS ต้องไม่น้อยกว่า 30"

[[ -f "tools/candidate_manifest.py" ]] || \
    fail "ต้องรันจาก repo root (ไม่พบ tools/candidate_manifest.py)"
[[ -f "firebase.json" ]] || fail "ไม่พบ firebase.json"
[[ -f "database.rules.json" ]] || fail "ไม่พบ database.rules.json"

if command -v firebase >/dev/null 2>&1; then
    FIREBASE_CMD=(firebase)
elif command -v npx >/dev/null 2>&1; then
    echo "Firebase CLI not found; using npx firebase-tools@latest"
    FIREBASE_CMD=(npx --yes firebase-tools@latest)
else
    fail "ไม่พบทั้ง firebase และ npx"
fi

DEPLOY_COMMIT="$(git rev-parse --verify HEAD 2>/dev/null || true)"
[[ "${DEPLOY_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || \
    fail "source ต้องเป็น Git checkout ที่มี HEAD commit"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    fail "Git working tree ไม่สะอาด — commit/stash การเปลี่ยนแปลงก่อน deploy"
fi
echo "Repo commit: ${DEPLOY_COMMIT}"

# -----------------------------------------------------------------------------
# 1. GOOGLE CLOUD + FIREBASE ACCESS
# -----------------------------------------------------------------------------

step "1/10 GOOGLE CLOUD + FIREBASE ACCESS"

ACCOUNT="$(
    gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -n1
)"
[[ -n "${ACCOUNT}" ]] || fail "ไม่มี active Google Cloud account; รัน gcloud auth login"

gcloud projects describe "${PROJECT_ID}" >/dev/null
gcloud config set project "${PROJECT_ID}" >/dev/null

PROJECT_NUMBER="$(
    gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)'
)"
[[ "${PROJECT_NUMBER}" =~ ^[0-9]+$ ]] || fail "อ่าน project number ไม่สำเร็จ"

echo "Account : ${ACCOUNT}"
echo "Project : ${PROJECT_ID} (${PROJECT_NUMBER})"

if ! FIREBASE_PROJECTS_JSON="$(
    "${FIREBASE_CMD[@]}" projects:list --json 2>/dev/null
)"; then
    fail "Firebase CLI เข้าใช้งานไม่ได้; รัน firebase login --no-localhost แล้วลองใหม่"
fi

if ! printf '%s' "${FIREBASE_PROJECTS_JSON}" | python3 -c '
import json
import sys

target = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

def walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    elif isinstance(value, str):
        yield value

raise SystemExit(0 if target in set(walk(data)) else 1)
' "${PROJECT_ID}"; then
    fail "Firebase account ไม่มีสิทธิ์เห็น project '${PROJECT_ID}'"
fi

echo "Firebase access: OK"

# -----------------------------------------------------------------------------
# 2. ENABLE REQUIRED APIS
# -----------------------------------------------------------------------------

step "2/10 ENABLE GOOGLE CLOUD APIS"

gcloud services enable \
    serviceusage.googleapis.com \
    cloudfunctions.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    logging.googleapis.com \
    firebase.googleapis.com \
    firebasedatabase.googleapis.com \
    compute.googleapis.com \
    storage.googleapis.com \
    --project="${PROJECT_ID}"

# -----------------------------------------------------------------------------
# 3. SERVICE ACCOUNTS
# -----------------------------------------------------------------------------

step "3/10 SERVICE ACCOUNTS"

ensure_service_account "${RUNTIME_SA_NAME}" "LEGO runtime UAT"
ensure_service_account "${SCHEDULER_SA_NAME}" "LEGO scheduler UAT"

BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${BUILD_SA}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
    fail "ไม่พบ build service account '${BUILD_SA}'; ตรวจ organization policy/default service accounts"
fi
echo "Build SA: ${BUILD_SA}"

# -----------------------------------------------------------------------------
# 4. SECRET MANAGER
# -----------------------------------------------------------------------------

step "4/10 SECRET MANAGER"

APP_KEY_SECRET="webull-app-key-${SUFFIX}"
APP_SECRET_SECRET="webull-app-secret-${SUFFIX}"
ACCOUNT_ID_SECRET="webull-account-id-${SUFFIX}"
TOKEN_SECRET="webull-token-${SUFFIX}"

for secret in \
    "${APP_KEY_SECRET}" \
    "${APP_SECRET_SECRET}" \
    "${ACCOUNT_ID_SECRET}" \
    "${TOKEN_SECRET}"
do
    ensure_secret_container "${secret}"
done

require_enabled_secret_version "${APP_KEY_SECRET}"
require_enabled_secret_version "${APP_SECRET_SECRET}"
require_enabled_secret_version "${ACCOUNT_ID_SECRET}"

for secret in \
    "${APP_KEY_SECRET}" \
    "${APP_SECRET_SECRET}" \
    "${ACCOUNT_ID_SECRET}" \
    "${TOKEN_SECRET}"
do
    gcloud secrets add-iam-policy-binding "${secret}" \
        --member="serviceAccount:${RUNTIME_SA}" \
        --role="roles/secretmanager.secretAccessor" \
        --project="${PROJECT_ID}" \
        --quiet >/dev/null
done

# -----------------------------------------------------------------------------
# 5. IAM
# -----------------------------------------------------------------------------

step "5/10 IAM"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/firebasedatabase.admin" \
    --quiet >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/logging.logWriter" \
    --quiet >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BUILD_SA}" \
    --role="roles/run.builder" \
    --quiet >/dev/null

# -----------------------------------------------------------------------------
# 6. RESOLVE REAL RTDB
# -----------------------------------------------------------------------------

step "6/10 RESOLVE FIREBASE REALTIME DATABASE"

if [[ -n "${DATABASE_URL_OVERRIDE}" ]]; then
    DATABASE_URL="${DATABASE_URL_OVERRIDE%/}"
    echo "Using DATABASE_URL_OVERRIDE: ${DATABASE_URL}"
else
    if ! DB_JSON="$(
        "${FIREBASE_CMD[@]}" database:instances:list \
            --project="${PROJECT_ID}" --json 2>/dev/null
    )"; then
        fail "อ่านรายการ RTDB ไม่สำเร็จ"
    fi

    DATABASE_URL="$(
        printf '%s' "${DB_JSON}" | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit

def walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    elif isinstance(value, str):
        yield value

urls = sorted({
    value.rstrip("/") for value in walk(data)
    if value.startswith("https://")
    and (".firebasedatabase.app" in value or ".firebaseio.com" in value)
})

default_urls = [url for url in urls if "-default-rtdb" in url]
if len(default_urls) == 1:
    print(default_urls[0])
elif len(urls) == 1:
    print(urls[0])
else:
    print("")
'
    )"
fi

[[ "${DATABASE_URL}" =~ ^https://[^[:space:],]+$ ]] || {
    echo "ไม่พบ RTDB URL จริงเพียงรายการเดียว จึงไม่เดา URL และไม่ deploy ต่อ"
    echo "สร้าง Realtime Database ใน Firebase Console ก่อน หรือเลือก URL โดยระบุ:"
    echo "  DATABASE_URL_OVERRIDE=https://... bash deploy/cloudshell-all-in-one.sh"
    exit 1
}

echo "RTDB: ${DATABASE_URL}"

# -----------------------------------------------------------------------------
# 7. CANDIDATE + PRESERVED RUNTIME SETTINGS
# -----------------------------------------------------------------------------

step "7/10 CANDIDATE + RUNTIME SETTINGS"

EXISTING_FUNCTION_JSON="$(
    gcloud functions describe "${FUNCTION_NAME}" \
        --gen2 \
        --region="${REGION}" \
        --project="${PROJECT_ID}" \
        --format=json 2>/dev/null || true
)"

SYMBOL="$(resolve_value "${LEGO_SYMBOL_OVERRIDE}" "$(read_existing_env LEGO_SYMBOL)" "AAPL")"
FIX_C="$(resolve_value "${LEGO_FIX_C_OVERRIDE}" "$(read_existing_env LEGO_FIX_C)" "3000")"
DIFF="$(resolve_value "${LEGO_DIFF_OVERRIDE}" "$(read_existing_env LEGO_DIFF)" "25")"
DNA_BUNDLE="$(resolve_value "${LEGO_DNA_BUNDLE_OVERRIDE}" "$(read_existing_env LEGO_DNA_BUNDLE)" "strategy.example.json")"

EXISTING_SCHEDULE="$(
    gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
        --location="${REGION}" \
        --project="${PROJECT_ID}" \
        --format='value(schedule)' 2>/dev/null || true
)"
SCHEDULE="$(resolve_value "${LEGO_SCHEDULE_OVERRIDE}" "${EXISTING_SCHEDULE}" "* * * * *")"

python3 - "${FIX_C}" "${DIFF}" <<'PY'
from decimal import Decimal, InvalidOperation
import sys

for name, raw in (("LEGO_FIX_C", sys.argv[1]), ("LEGO_DIFF", sys.argv[2])):
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise SystemExit(f"ERROR: {name} must be numeric")
    if not value.is_finite() or value <= 0:
        raise SystemExit(f"ERROR: {name} must be finite and > 0")
PY

reject_comma "LEGO_SYMBOL" "${SYMBOL}"
reject_comma "LEGO_FIX_C" "${FIX_C}"
reject_comma "LEGO_DIFF" "${DIFF}"
reject_comma "LEGO_DNA_BUNDLE" "${DNA_BUNDLE}"
[[ -f "${DNA_BUNDLE}" ]] || fail "ไม่พบ DNA bundle '${DNA_BUNDLE}'"

DNA_REPORT="$(python3 - "${DNA_BUNDLE}" <<'PY'
import json
import sys
from pathlib import Path

from config import DNABundle
from dna_engine import decode_dna

path = Path(sys.argv[1])
raw = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(raw, dict):
    raise SystemExit("ERROR: DNA bundle must be a JSON object")
bundle = DNABundle.from_mapping(raw)
decoded = decode_dna(bundle.dna_code)
print(json.dumps({
    "fingerprint": bundle.decoded_array_sha256,
    "length": len(decoded),
    "ones": sum(decoded),
}, separators=(",", ":")))
PY
)" || fail "ตรวจ DNA bundle ไม่ผ่าน; ติดตั้ง requirements.txt แล้วตรวจไฟล์อีกครั้ง"

CANDIDATE_HASH="$(
    python3 tools/candidate_manifest.py | \
        python3 -c 'import json, sys; print(json.load(sys.stdin)["candidate_hash"])'
)"
require_sha256 "candidate hash" "${CANDIDATE_HASH}"

if [[ -n "${EXPECTED_CANDIDATE_HASH}" ]]; then
    require_sha256 "EXPECTED_CANDIDATE_HASH" "${EXPECTED_CANDIDATE_HASH}"
    [[ "${CANDIDATE_HASH}" == "${EXPECTED_CANDIDATE_HASH}" ]] || \
        fail "candidate hash ไม่ตรงกับ release evidence ที่อนุมัติ"
    CANDIDATE_STATUS="verified"
else
    CANDIDATE_STATUS="self-certified; guarded deployment only"
fi

if [[ -n "${RELEASE_AUTHORIZATION_OVERRIDE}" ]]; then
    require_sha256 "LEGO_RELEASE_AUTHORIZATION_OVERRIDE" \
        "${RELEASE_AUTHORIZATION_OVERRIDE}"
fi

# Validate the supplied release binding without ever printing the account ID.
EXPECTED_RELEASE_BINDING="$(
    gcloud secrets versions access latest \
        --secret="${ACCOUNT_ID_SECRET}" \
        --project="${PROJECT_ID}" | \
    python3 -c '
import hashlib
import sys

environment, candidate = sys.argv[1:3]
account_id = sys.stdin.read().strip()
if not account_id:
    raise SystemExit("WEBULL account secret is empty")
account_fingerprint = hashlib.sha256(
    f"webull-runtime-v2\0{environment}\0{account_id}".encode()
).hexdigest()
print(hashlib.sha256(
    f"lego-release-v2\0{environment}\0{account_fingerprint}\0{candidate}".encode()
).hexdigest())
' "${ENVIRONMENT}" "${CANDIDATE_HASH}"
)"
require_sha256 "computed release binding" "${EXPECTED_RELEASE_BINDING}"

ORDER_SUBMISSION_EXPECTED="false"
if [[ "${MODE}" == "trade" && "${ACTIVE}" == "true" ]]; then
    [[ -n "${EXPECTED_CANDIDATE_HASH}" ]] || \
        fail "trade+active ต้องระบุ EXPECTED_CANDIDATE_HASH จาก release evidence"
    [[ -n "${RELEASE_AUTHORIZATION_OVERRIDE}" ]] || \
        fail "trade+active ต้องระบุ LEGO_RELEASE_AUTHORIZATION_OVERRIDE"
    [[ "${RELEASE_AUTHORIZATION_OVERRIDE}" == "${EXPECTED_RELEASE_BINDING}" ]] || \
        fail "release authorization ไม่ตรงกับ environment/account/candidate นี้"
    ORDER_SUBMISSION_EXPECTED="true"
elif [[ -n "${RELEASE_AUTHORIZATION_OVERRIDE}" \
        && "${RELEASE_AUTHORIZATION_OVERRIDE}" != "${EXPECTED_RELEASE_BINDING}" ]]; then
    fail "release authorization ไม่ตรงกับ environment/account/candidate นี้"
fi

TOKEN_SECRET_RESOURCE=""
if [[ -n "${TOKEN_SECRET_RESOURCE_OVERRIDE}" ]]; then
    if [[ "${TOKEN_SECRET_RESOURCE_OVERRIDE}" =~ ^projects/([^/]+)/secrets/([^/]+)(/versions/[^/]+)?$ ]]; then
        TOKEN_PROJECT="${BASH_REMATCH[1]}"
        TOKEN_NAME="${BASH_REMATCH[2]}"
        TOKEN_VERSION="${BASH_REMATCH[3]:-/versions/latest}"
        TOKEN_VERSION="${TOKEN_VERSION#/versions/}"
    else
        fail "WEBULL_TOKEN_SECRET_OVERRIDE ต้องเป็น projects/PROJECT/secrets/NAME[/versions/VERSION]"
    fi
    TOKEN_STATE="$(
        gcloud secrets versions describe "${TOKEN_VERSION}" \
            --secret="${TOKEN_NAME}" \
            --project="${TOKEN_PROJECT}" \
            --format='value(state)' 2>/dev/null || true
    )"
    [[ "${TOKEN_STATE}" == "ENABLED" ]] || \
        fail "Webull token secret latest version ไม่มีหรือไม่ได้ ENABLED"
    gcloud secrets add-iam-policy-binding "${TOKEN_NAME}" \
        --member="serviceAccount:${RUNTIME_SA}" \
        --role="roles/secretmanager.secretAccessor" \
        --project="${TOKEN_PROJECT}" \
        --quiet >/dev/null
    TOKEN_SECRET_RESOURCE="${TOKEN_SECRET_RESOURCE_OVERRIDE}"
fi

echo "Symbol         : ${SYMBOL}"
echo "Principal      : ${FIX_C}"
echo "Diff           : ${DIFF}"
echo "DNA bundle     : ${DNA_BUNDLE}"
echo "DNA report     : ${DNA_REPORT}"
echo "Schedule       : ${SCHEDULE} UTC"
echo "Candidate hash : ${CANDIDATE_HASH} (${CANDIDATE_STATUS})"
echo "Mode           : ${MODE}"
echo "Active         : ${ACTIVE}"
echo "Order submit   : ${ORDER_SUBMISSION_EXPECTED}"
if [[ -n "${TOKEN_SECRET_RESOURCE}" ]]; then
    echo "Token source   : Secret Manager"
else
    echo "Token source   : broker HMAC/token policy (no token secret requested)"
fi

# -----------------------------------------------------------------------------
# 8. DEPLOY DATABASE RULES + CLOUD FUNCTION GEN2
# -----------------------------------------------------------------------------

step "8/10 DEPLOY DATABASE RULES + CLOUD FUNCTION GEN2"

# All source, DNA, candidate, credential and release checks above must pass
# before this script changes Firebase rules or the Cloud Function.
"${FIREBASE_CMD[@]}" deploy \
    --only database \
    --project="${PROJECT_ID}" \
    --non-interactive

ENV_VARS="WEBULL_ENV=${ENVIRONMENT},FIREBASE_DB_URL=${DATABASE_URL},LEGO_SYMBOL=${SYMBOL},LEGO_FIX_C=${FIX_C},LEGO_DIFF=${DIFF},LEGO_DNA_BUNDLE=${DNA_BUNDLE},LEGO_MODE=${MODE},LEGO_ACTIVE=${ACTIVE},LEGO_CANDIDATE_HASH=${CANDIDATE_HASH},LEGO_RELEASE_AUTHORIZATION=${RELEASE_AUTHORIZATION_OVERRIDE},WEBULL_TOKEN_DIR=/tmp/webull_token,LEGO_DNA_CLOCK_MODE=market"

if [[ -n "${TOKEN_SECRET_RESOURCE}" ]]; then
    ENV_VARS="${ENV_VARS},WEBULL_TOKEN_SECRET=${TOKEN_SECRET_RESOURCE}"
fi

SECRET_BINDINGS="WEBULL_APP_KEY=${APP_KEY_SECRET}:latest,WEBULL_APP_SECRET=${APP_SECRET_SECRET}:latest,WEBULL_ACCOUNT_ID=${ACCOUNT_ID_SECRET}:latest"

gcloud functions deploy "${FUNCTION_NAME}" \
    --gen2 \
    --runtime=python312 \
    --region="${REGION}" \
    --source=. \
    --entry-point=lego_tick \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="${RUNTIME_SA}" \
    --memory=512Mi \
    --timeout=45s \
    --concurrency=1 \
    --max-instances=1 \
    --min-instances=0 \
    --set-env-vars="${ENV_VARS}" \
    --set-secrets="${SECRET_BINDINGS}" \
    --project="${PROJECT_ID}" \
    --quiet

FUNCTION_URI="$(
    gcloud functions describe "${FUNCTION_NAME}" \
        --gen2 \
        --region="${REGION}" \
        --project="${PROJECT_ID}" \
        --format='value(serviceConfig.uri)'
)"
[[ "${FUNCTION_URI}" =~ ^https:// ]] || fail "อ่าน Function HTTPS URI ไม่สำเร็จ"
echo "Function URL: ${FUNCTION_URI}"

# -----------------------------------------------------------------------------
# 9. INVOKER + CLOUD SCHEDULER
# -----------------------------------------------------------------------------

step "9/10 INVOKER + CLOUD SCHEDULER"

gcloud functions add-invoker-policy-binding "${FUNCTION_NAME}" \
    --gen2 \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${SCHEDULER_SA}" \
    --quiet

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
    SCHEDULER_VERB="update"
else
    SCHEDULER_VERB="create"
fi

gcloud scheduler jobs "${SCHEDULER_VERB}" http "${SCHEDULER_JOB}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --schedule="${SCHEDULE}" \
    --time-zone="UTC" \
    --uri="${FUNCTION_URI}" \
    --http-method=POST \
    --oidc-service-account-email="${SCHEDULER_SA}" \
    --oidc-token-audience="${FUNCTION_URI}" \
    --max-retry-attempts=0 \
    --quiet

# -----------------------------------------------------------------------------
# 10. SMOKE TRIGGER + FINAL STATUS
# -----------------------------------------------------------------------------

step "10/10 SMOKE TRIGGER + FINAL STATUS"

REVISION="$(
    gcloud run services describe "${FUNCTION_NAME}" \
        --region="${REGION}" \
        --project="${PROJECT_ID}" \
        --format='value(status.latestReadyRevisionName)'
)"
[[ -n "${REVISION}" ]] || fail "อ่าน latest ready revision ไม่สำเร็จ"

SMOKE_STARTED="$(python3 -c \
    'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
echo "Triggering smoke tick for revision ${REVISION}..."
gcloud scheduler jobs run "${SCHEDULER_JOB}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}"

SMOKE_FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${FUNCTION_NAME}\" AND resource.labels.revision_name=\"${REVISION}\" AND jsonPayload.event=\"lego_tick_completed\" AND timestamp>=\"${SMOKE_STARTED}\""
SMOKE_LOG="[]"
SMOKE_DEADLINE=$((SECONDS + SMOKE_TIMEOUT_SECONDS))

while (( SECONDS < SMOKE_DEADLINE )); do
    SMOKE_LOG="$(
        gcloud logging read "${SMOKE_FILTER}" \
            --project="${PROJECT_ID}" \
            --order=desc \
            --limit=1 \
            --format=json 2>/dev/null || true
    )"
    if [[ -n "${SMOKE_LOG}" && "${SMOKE_LOG}" != "[]" ]]; then
        break
    fi
    sleep 5
done

[[ -n "${SMOKE_LOG}" && "${SMOKE_LOG}" != "[]" ]] || \
    fail "ไม่พบ structured smoke event ของ revision ${REVISION} ภายใน ${SMOKE_TIMEOUT_SECONDS} วินาที"

if ! SMOKE_SUMMARY="$(
    printf '%s' "${SMOKE_LOG}" | python3 -c '
import json
import sys

candidate, revision, mode, active, order_expected = sys.argv[1:6]
entries = json.load(sys.stdin)
if not isinstance(entries, list) or not entries:
    raise SystemExit("missing smoke log entry")
payload = entries[0].get("jsonPayload") or {}
required = ("event", "candidate_hash", "http_status", "business_status")
missing = [key for key in required if payload.get(key) is None]
if missing:
    raise SystemExit("smoke event missing fields: " + ",".join(missing))
if payload.get("event") != "lego_tick_completed":
    raise SystemExit("unexpected smoke event")
if payload.get("candidate_hash") != candidate:
    raise SystemExit("smoke candidate hash mismatch")
if payload.get("revision") not in (None, "", revision):
    raise SystemExit("smoke revision mismatch")
if payload.get("mode") != mode or str(payload.get("active")).lower() != active:
    raise SystemExit("smoke operator settings mismatch")
http_status = int(payload["http_status"])
business_status = str(payload["business_status"])
bad = {
    "ERROR", "FEE_OVERDUE", "MANUAL_RECONCILIATION_REQUIRED",
}
if http_status >= 400 or business_status in bad:
    raise SystemExit(
        f"unhealthy smoke: http={http_status} business={business_status}")
if order_expected == "true" and business_status == "INTENT_BLOCKED":
    raise SystemExit("release/token preflight blocked an authorized trade deployment")
print(json.dumps({
    "revision": revision,
    "http_status": http_status,
    "business_status": business_status,
    "pipeline_status": payload.get("pipeline_status"),
    "correlation_id": payload.get("correlation_id"),
}, separators=(",", ":")))
' "${CANDIDATE_HASH}" "${REVISION}" "${MODE}" "${ACTIVE}" \
      "${ORDER_SUBMISSION_EXPECTED}"
)"; then
    fail "structured smoke event ไม่ผ่าน validation"
fi

if [[ "${ORDER_SUBMISSION_EXPECTED}" == "true" ]]; then
    ORDER_STATUS="ENABLED (explicit release binding verified)"
else
    ORDER_STATUS="BLOCKED (mode/active/release gate)"
fi

echo
echo "============================================================"
echo " LEGO UAT DEPLOY COMPLETE"
echo "============================================================"
echo "Project      : ${PROJECT_ID}"
echo "Environment  : ${ENVIRONMENT}"
echo "Region       : ${REGION}"
echo "Git commit   : ${DEPLOY_COMMIT}"
echo "Candidate    : ${CANDIDATE_HASH} (${CANDIDATE_STATUS})"
echo "Function     : ${FUNCTION_NAME}"
echo "Revision     : ${REVISION}"
echo "URL          : ${FUNCTION_URI}"
echo "Scheduler    : ${SCHEDULER_JOB}"
echo "Schedule     : ${SCHEDULE} UTC"
echo "RTDB         : ${DATABASE_URL}"
echo "Runtime SA   : ${RUNTIME_SA}"
echo "Scheduler SA : ${SCHEDULER_SA}"
echo "Build SA     : ${BUILD_SA}"
echo "LEGO_SYMBOL  : ${SYMBOL}"
echo "LEGO_FIX_C   : ${FIX_C}"
echo "LEGO_MODE    : ${MODE}"
echo "LEGO_ACTIVE  : ${ACTIVE}"
echo "Smoke        : ${SMOKE_SUMMARY}"
echo "STATUS       : ORDER SUBMISSION = ${ORDER_STATUS}"
echo "============================================================"

echo
echo "Cloud Function:"
gcloud functions describe "${FUNCTION_NAME}" \
    --gen2 \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='table(name,state,serviceConfig.uri,updateTime)'

echo
echo "Cloud Scheduler:"
gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='yaml(name,state,schedule,timeZone,httpTarget.uri)'

echo
echo "Recent LEGO logs:"
gcloud logging read \
    "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${FUNCTION_NAME}\"" \
    --project="${PROJECT_ID}" \
    --freshness=10m \
    --limit=20 \
    --format='table(timestamp,severity,textPayload,jsonPayload.business_status,jsonPayload.event)' \
    || true

echo
echo "DONE. UAT deployment and smoke verification succeeded."
