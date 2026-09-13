#!/usr/bin/env bash

set -Eeuo pipefail

# LEGO PRINCIPAL v2 — Google Cloud Shell all-in-one UAT deploy
#
# Safe invariants:
#   environment = UAT
#   mode        = trade
#   active      = true
#   Webull token secret is NOT bound
#
# Run from the repository root:
#   bash deploy/cloudshell-all-in-one.sh
#
# Optional overrides:
#   DATABASE_URL_OVERRIDE=https://...
#   EXPECTED_CANDIDATE_HASH=<approved sha256>
#   LEGO_SYMBOL_OVERRIDE=AAPL
#   LEGO_FIX_C_OVERRIDE=3000
#   LEGO_DIFF_OVERRIDE=25
#   LEGO_DNA_BUNDLE_OVERRIDE=strategy.example.json
#   LEGO_SCHEDULE_OVERRIDE='* * * * *'

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
readonly MODE="observe"
readonly ACTIVE="false"

readonly DATABASE_URL_OVERRIDE="${DATABASE_URL_OVERRIDE:-}"
readonly EXPECTED_CANDIDATE_HASH="${EXPECTED_CANDIDATE_HASH:-}"
readonly LEGO_SYMBOL_OVERRIDE="${LEGO_SYMBOL_OVERRIDE:-}"
readonly LEGO_FIX_C_OVERRIDE="${LEGO_FIX_C_OVERRIDE:-}"
readonly LEGO_DIFF_OVERRIDE="${LEGO_DIFF_OVERRIDE:-}"
readonly LEGO_DNA_BUNDLE_OVERRIDE="${LEGO_DNA_BUNDLE_OVERRIDE:-}"
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
        echo "ตัวอย่างคำสั่ง (อย่าใส่ค่าลับลงใน command history):"
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
echo "Repo commit: ${DEPLOY_COMMIT:-unknown}"

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

# Source deployment uses the Compute default service account for builds in
# current Google Cloud projects. Make the identity explicit and fail early if an
# organization policy prevented its creation.
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
# 6. RESOLVE REAL RTDB + DEPLOY RULES
# -----------------------------------------------------------------------------

step "6/10 FIREBASE REALTIME DATABASE"

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
    # Empty and ambiguous results both require an explicit override.
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

"${FIREBASE_CMD[@]}" deploy \
    --only database \
    --project="${PROJECT_ID}" \
    --non-interactive

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
FIX_C="$(resolve_value "${LEGO_FIX_C_OVERRIDE}" "$(read_existing_env LEGO_FIX_C)" "1500")"
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

CANDIDATE_HASH="$(
    python3 tools/candidate_manifest.py | \
        python3 -c 'import json, sys; print(json.load(sys.stdin)["candidate_hash"])'
)"
[[ "${CANDIDATE_HASH}" =~ ^[0-9a-f]{64}$ ]] || fail "candidate hash ไม่ถูกต้อง"

if [[ -n "${EXPECTED_CANDIDATE_HASH}" ]]; then
    [[ "${EXPECTED_CANDIDATE_HASH}" =~ ^[0-9a-f]{64}$ ]] || \
        fail "EXPECTED_CANDIDATE_HASH ต้องเป็น SHA-256 ตัวพิมพ์เล็ก 64 ตัว"
    [[ "${CANDIDATE_HASH}" == "${EXPECTED_CANDIDATE_HASH}" ]] || \
        fail "candidate hash ไม่ตรงกับ release evidence ที่อนุมัติ"
    CANDIDATE_STATUS="verified"
else
    CANDIDATE_STATUS="self-certified for UAT/observe"
fi

echo "Symbol         : ${SYMBOL}"
echo "Principal      : ${FIX_C}"
echo "Diff           : ${DIFF}"
echo "DNA bundle     : ${DNA_BUNDLE}"
echo "Schedule       : ${SCHEDULE} UTC"
echo "Candidate hash : ${CANDIDATE_HASH} (${CANDIDATE_STATUS})"
echo "Mode           : ${MODE}"
echo "Active         : ${ACTIVE}"

# -----------------------------------------------------------------------------
# 8. DEPLOY CLOUD FUNCTION GEN2
# -----------------------------------------------------------------------------

step "8/10 DEPLOY CLOUD FUNCTION GEN2"

ENV_VARS="WEBULL_ENV=${ENVIRONMENT},FIREBASE_DB_URL=${DATABASE_URL},LEGO_SYMBOL=${SYMBOL},LEGO_FIX_C=${FIX_C},LEGO_DIFF=${DIFF},LEGO_DNA_BUNDLE=${DNA_BUNDLE},LEGO_MODE=${MODE},LEGO_ACTIVE=${ACTIVE},LEGO_CANDIDATE_HASH=${CANDIDATE_HASH},LEGO_RELEASE_AUTHORIZATION=,WEBULL_TOKEN_DIR=/tmp/webull_token,LEGO_DNA_CLOCK_MODE=market"

SECRET_BINDINGS="WEBULL_APP_KEY=${APP_KEY_SECRET}:latest,WEBULL_APP_SECRET=${APP_SECRET_SECRET}:latest,WEBULL_ACCOUNT_ID=${ACCOUNT_ID_SECRET}:latest"

# WEBULL_TOKEN_SECRET is intentionally absent. UAT applications reporting
# _check_token_enable=False use HMAC credentials and must not be forced to
# hydrate an empty token secret at cold start.
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
# 10. SAFE SMOKE TRIGGER + FINAL STATUS
# -----------------------------------------------------------------------------

step "10/10 SAFE SMOKE TRIGGER + FINAL STATUS"

gcloud scheduler jobs run "${SCHEDULER_JOB}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}"

echo
echo "============================================================"
echo " LEGO UAT DEPLOY COMPLETE"
echo "============================================================"
echo "Project      : ${PROJECT_ID}"
echo "Environment  : ${ENVIRONMENT}"
echo "Region       : ${REGION}"
echo "Git commit   : ${DEPLOY_COMMIT:-unknown}"
echo "Candidate    : ${CANDIDATE_HASH} (${CANDIDATE_STATUS})"
echo "Function     : ${FUNCTION_NAME}"
echo "URL          : ${FUNCTION_URI}"
echo "Scheduler    : ${SCHEDULER_JOB}"
echo "Schedule     : ${SCHEDULE} UTC"
echo "RTDB         : ${DATABASE_URL}"
echo "Runtime SA   : ${RUNTIME_SA}"
echo "Scheduler SA : ${SCHEDULER_SA}"
echo "Build SA     : ${BUILD_SA}"
echo "LEGO_MODE    : ${MODE}"
echo "LEGO_ACTIVE  : ${ACTIVE}"
echo "ORDER SUBMISSION = DISABLED"
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
echo "Recent LEGO logs (the scheduler trigger is asynchronous):"
gcloud logging read \
    "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${FUNCTION_NAME}\"" \
    --project="${PROJECT_ID}" \
    --freshness=10m \
    --limit=20 \
    --format='table(timestamp,severity,textPayload,jsonPayload.business_status,jsonPayload.event)' \
    || true

echo
echo "DONE. Trading remains disabled: observe + active=false."
