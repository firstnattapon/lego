#!/usr/bin/env bash
# PRODUCTION observe/inactive in one Cloud Shell run: the real account and real quotes,
# never an order (docs/PROD_OBSERVE_TH.md). From the repository root:
#
#   bash deploy/cloudshell-prod-observe.sh
#   LEGO_MODE_OVERRIDE=preview bash deploy/cloudshell-prod-observe.sh
#   LEGO_MODE_OVERRIDE=paper bash deploy/cloudshell-prod-observe.sh
#
# preview runs the whole order path of a real release, the Preview of each real order
# included, and never calls Place. It also asks for the last price (share cap), whether
# the account already holds about FIX_C, and Webull's confirmation that Preview creates
# no order; caps, a 5-session window and the authorization come from ops.py release-plan.
#
# paper is preview that books, at the point preview stops, the fill a market order would
# have had: paper holdings move and the 17-column ledger runs, Place is still never called.
# Its chain lives in a database of its own (lego-firebase-paper), never in lego-firebase-prod,
# and starts from zero paper shares, so it asks no funding question.
#
# It asks only for what is missing: FIX_C, the PROD app key/secret and account id (first
# run) and, when the token is missing or has 3 days or less left, the approval of the
# OpenAPI login in the Webull app. Then it puts the PROD Realtime Database in place
# (separate from UAT, public reads denied) and runs deploy/cloudshell-all-in-one.sh as
# PROD + observe + inactive. Real money is a separate, acknowledged release
# (docs/PROD_LIVE_RUNBOOK_TH.md); nothing here can open it.
set -Eeuo pipefail

readonly PROJECT_ID="lego-firebase"
readonly SUFFIX="prod"
readonly MODE="${LEGO_MODE_OVERRIDE:-observe}"
# Paper holdings are fake: they get a database of their own, so lego-firebase-prod holds
# only what the real account did.
if [[ "${MODE}" == paper ]]; then DEFAULT_DB_INSTANCE="lego-firebase-paper"
else DEFAULT_DB_INSTANCE="lego-firebase-${SUFFIX}"; fi
readonly DB_INSTANCE="${LEGO_PROD_DB_INSTANCE:-${DEFAULT_DB_INSTANCE}}"
readonly DB_LOCATION="asia-southeast1"
# Same names as deploy/cloudshell-all-in-one.sh gives WEBULL_ENV=PROD.
readonly APP_KEY_SECRET="webull-app-key-${SUFFIX}"
readonly APP_SECRET_SECRET="webull-app-secret-${SUFFIX}"
readonly ACCOUNT_SECRET="webull-account-id-${SUFFIX}"
readonly TOKEN_SECRET="webull-token-${SUFFIX}"
readonly SYMBOL="${LEGO_SYMBOL_OVERRIDE:-UBER}"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

step() {
    echo
    echo "== $*"
}

[[ -f deploy/cloudshell-all-in-one.sh && -f database.rules.prod.json ]] || \
    fail "run from the repository root"
# The deploy refuses a dirty checkout; say so before anyone approves a token for nothing.
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || \
    fail "commit or stash local changes first: the deploy needs a clean checkout"

# The one decision: the principal the chain is keyed on. Observe never spends it.
FIX_C="${LEGO_FIX_C_OVERRIDE:-}"
if [[ -z "${FIX_C}" ]]; then
    read -rp "LEGO_FIX_C (USD; observe never spends it): " FIX_C || \
        fail "LEGO_FIX_C_OVERRIDE is required"
fi
[[ "${FIX_C}" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v v="${FIX_C}" 'BEGIN { exit !(v > 0) }' || \
    fail "LEGO_FIX_C must be a number > 0"
[[ "${DB_INSTANCE}" =~ ^[a-z0-9-]+$ ]] || fail "LEGO_PROD_DB_INSTANCE must be a Realtime Database instance name"
[[ "${DB_INSTANCE}" != *-default-rtdb ]] || \
    fail "${DB_INSTANCE} is a default instance, the database UAT uses; name another with LEGO_PROD_DB_INSTANCE"
[[ "${MODE}" == observe || "${MODE}" == preview || "${MODE}" == paper ]] || \
    fail "LEGO_MODE_OVERRIDE must be observe, preview or paper here; real money is docs/PROD_LIVE_RUNBOOK_TH.md"
# The deployed revision is checked for the same pairing (tools/verify_deployment.py).
if [[ "${MODE}" == paper ]]; then
    [[ "${DB_INSTANCE}" == *paper* ]] || \
        fail "paper keeps its fake holdings in a database named for paper; set LEGO_PROD_DB_INSTANCE to a name with 'paper' in it"
else
    [[ "${DB_INSTANCE}" != *paper* ]] || \
        fail "${DB_INSTANCE} holds paper holdings; ${MODE} keeps its chain in lego-firebase-prod"
fi

if [[ "${MODE}" == preview || "${MODE}" == paper ]]; then
    PRICE="${LEGO_REFERENCE_PRICE:-}"
    if [[ -z "${PRICE}" ]]; then
        read -rp "Last price of ${SYMBOL} in USD (sets the share cap): " PRICE || \
            fail "LEGO_REFERENCE_PRICE is required for ${MODE}"
    fi
    [[ "${PRICE}" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v v="${PRICE}" 'BEGIN { exit !(v > 0) }' || \
        fail "the reference price must be a number > 0"
    # A flat account's first order is the whole FIX_C: only the funding caps let it reach Preview.
    FUNDING="${LEGO_FUNDING_MODE_OVERRIDE:-}"
    if [[ "${MODE}" == paper ]]; then
        # Paper starts from zero paper shares whatever the real account holds.
        FUNDING=initial-funding
    elif [[ -z "${FUNDING}" ]]; then
        held=""
        read -rp "Does the PROD account already hold about FIX_C of ${SYMBOL}? [y/N] " held || true
        if [[ "${held}" =~ ^[Yy] ]]; then FUNDING=prefunded; else FUNDING=initial-funding; fi
    fi
    [[ "${FUNDING}" == prefunded || "${FUNDING}" == initial-funding ]] || \
        fail "LEGO_FUNDING_MODE_OVERRIDE must be prefunded or initial-funding"
    # Preview runs on every READY slot. That it never creates an order rests on Webull's
    # documentation index only (docs/PROD_LIVE_RUNBOOK_TH.md section 11).
    CONFIRMED="${LEGO_PREVIEW_CONFIRMED:-}"
    if [[ "${CONFIRMED}" != yes ]]; then
        read -rp "Has Webull confirmed that Preview Order never creates an order? Type yes: " CONFIRMED || true
    fi
    [[ "${CONFIRMED}" == yes ]] || \
        fail "confirm with Webull first that Preview creates no order (docs/PROD_LIVE_RUNBOOK_TH.md section 11)"
fi

WORK="$(mktemp -d)"
chmod 700 "${WORK}"
trap 'rm -rf "${WORK}"' EXIT

# The pinned requirements (the Webull SDK is the wheel in vendor/), in a venv when the
# system Python lacks them. deploy/cloudshell-all-in-one.sh uses the same python3.
if ! python3 -c 'import numpy, webull, google.cloud.secretmanager' >/dev/null 2>&1; then
    if [[ ! -x "${HOME}/.lego-venv/bin/python3" ]]; then
        step "Python: installing requirements.txt into ~/.lego-venv (first run only)"
        python3 -m venv "${HOME}/.lego-venv"
        "${HOME}/.lego-venv/bin/pip" install -q -r requirements.txt
    fi
    export PATH="${HOME}/.lego-venv/bin:${PATH}"
    python3 -c 'import numpy, webull, google.cloud.secretmanager' >/dev/null 2>&1 || \
        fail "Python requirements missing; run: ~/.lego-venv/bin/pip install -r requirements.txt"
fi

if command -v firebase >/dev/null 2>&1; then
    FIREBASE=(firebase)
else
    command -v npx >/dev/null 2>&1 || fail "neither firebase nor npx is installed"
    FIREBASE=(npx --yes firebase-tools@latest)
fi

has_version() {
    [[ "$(gcloud secrets versions describe latest --secret="$1" --project="${PROJECT_ID}" \
        --format='value(state)' 2>/dev/null || true)" == ENABLED ]]
}

ensure_container() {
    gcloud secrets describe "$1" --project="${PROJECT_ID}" >/dev/null 2>&1 || \
        gcloud secrets create "$1" --replication-policy=automatic --project="${PROJECT_ID}" >/dev/null
}

add_version() {  # the value comes on stdin and never reaches a command line or the screen
    gcloud secrets versions add "$1" --data-file=- --project="${PROJECT_ID}" >/dev/null
}

ask_secret() {
    ensure_container "$1"
    has_version "$1" && return 0
    local value
    read -rsp "$2: " value || fail "$2 is required"
    echo
    [[ -n "${value}" ]] || fail "$2 is empty"
    printf '%s' "${value}" | add_version "$1"
}

stored_token_is_fresh() {  # the expiry stored with the token when it was issued: NORMAL, >3 days left
    gcloud secrets versions access latest --secret="${TOKEN_SECRET}" --project="${PROJECT_ID}" \
        2>/dev/null | python3 -c '
import sys, time
lines = sys.stdin.read().splitlines()
try:
    expires = float(lines[1])
except (IndexError, ValueError):
    sys.exit(1)
expires = expires / 1000 if expires > 1e11 else expires  # the API documents milliseconds
sys.exit(0 if len(lines) == 3 and lines[2] == "NORMAL" and expires > time.time() + 3 * 86400 else 1)'
}

token_is_fresh() {  # Webull's Check Token decides; the stored expiry only when Webull cannot be asked
    # Check Token answers the expiry Webull holds for the token now, which can be later
    # than the one stored with it at issue (a token is also INVALID after 15 days
    # without an API call). The token only passes through a pipe and the app
    # credentials through the environment, never argv.
    local key secret report status=0
    key="$(gcloud secrets versions access latest --secret="${APP_KEY_SECRET}" --project="${PROJECT_ID}")"
    secret="$(gcloud secrets versions access latest --secret="${APP_SECRET_SECRET}" --project="${PROJECT_ID}")"
    report="$(gcloud secrets versions access latest --secret="${TOKEN_SECRET}" --project="${PROJECT_ID}" \
            2>/dev/null | WEBULL_ENV=PROD WEBULL_APP_KEY="${key}" WEBULL_APP_SECRET="${secret}" \
            python3 ops.py token-status)" || status=$?
    case "${status}" in
        0|1)
            printf '%s' "${report}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("  Webull Check Token: status=%s, expires=%s, days_left=%s"
      % (r.get("status"), r.get("live_expires_at"), r.get("days_left")))' || true
            return "${status}" ;;
        *)
            echo "  Webull Check Token could not be asked; judging by the expiry stored with the token"
            stored_token_is_fresh ;;
    esac
}

issue_token() {
    # The function cannot do this itself: a PROD cold start refuses interactive token
    # creation (webull_io.build_clients), because only a person can approve it.
    local dir="${WORK}/token" key secret
    mkdir -m 700 "${dir}"
    key="$(gcloud secrets versions access latest --secret="${APP_KEY_SECRET}" --project="${PROJECT_ID}")"
    secret="$(gcloud secrets versions access latest --secret="${APP_SECRET_SECRET}" --project="${PROJECT_ID}")"
    echo "Open the Webull app now and approve the OpenAPI login (5 minutes)..."
    WEBULL_APP_KEY="${key}" WEBULL_APP_SECRET="${secret}" TOKEN_DIR="${dir}" python3 - <<'PY'
import os

os.chdir(os.environ["TOKEN_DIR"])  # the SDK's log file stays out of the repository
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

api = ApiClient(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "th")
api.add_endpoint("th", "api.webull.co.th")
api.set_token_dir(os.environ["TOKEN_DIR"])
trade = TradeClient(api)  # creates the token, then waits until it is approved in the app
try:  # only a convenience: without it the account_id is asked for; the token is kept either way
    payload = trade.account_v2.get_account_list().json()
    items = payload.get("data") if isinstance(payload, dict) else payload
    ids = [str(item["account_id"]) for item in items or []
           if isinstance(item, dict) and item.get("account_id")]
except Exception:
    ids = []
with open("account_ids", "w", encoding="utf-8") as handle:
    handle.write("\n".join(ids))
PY
    [[ -s "${dir}/token.txt" ]] || \
        fail "Webull issued no token for this app (token check off?); PROD needs one"
    # Dry run: only checks the token/expires/NORMAL format; gcloud stores it.
    python3 ops.py bootstrap-auth --token-file "${dir}/token.txt" \
        --secret "projects/${PROJECT_ID}/secrets/${TOKEN_SECRET}" >/dev/null
    add_version "${TOKEN_SECRET}" < "${dir}/token.txt"
}

step "1/4 PROD credentials and token (Secret Manager)"
ask_secret "${APP_KEY_SECRET}" "PROD Webull app key"
ask_secret "${APP_SECRET_SECRET}" "PROD Webull app secret"
ensure_container "${TOKEN_SECRET}"
if token_is_fresh; then
    echo "OK token: NORMAL, more than 3 days left"
else
    issue_token
    token_is_fresh || fail "the new token is not NORMAL with more than 3 days left; run again"
    echo "OK token: stored in ${TOKEN_SECRET}"
fi
ensure_container "${ACCOUNT_SECRET}"
if ! has_version "${ACCOUNT_SECRET}"; then
    if [[ -f "${WORK}/token/account_ids" && "$(grep -c . "${WORK}/token/account_ids")" == 1 ]]; then
        tr -d '\n' < "${WORK}/token/account_ids" | add_version "${ACCOUNT_SECRET}"
        echo "OK account: stored the only account_id this app can see"
    else
        ask_secret "${ACCOUNT_SECRET}" "PROD Webull account_id (not the account number)"
    fi
fi

step "2/4 PROD Realtime Database ${DB_INSTANCE} (separate from UAT)"
"${FIREBASE[@]}" projects:list >/dev/null 2>&1 || \
    fail "Firebase CLI is not signed in; run: firebase login --no-localhost"
instance_url() {
    "${FIREBASE[@]}" database:instances:list --project="${PROJECT_ID}" --json 2>/dev/null | \
        python3 -c '
import json, sys

def walk(value):
    if isinstance(value, dict):
        if value.get("name") == sys.argv[1] and str(value.get("databaseUrl", "")).startswith("https://"):
            print(value["databaseUrl"].rstrip("/"))
            raise SystemExit
        for item in value.values():
            walk(item)
    elif isinstance(value, list):
        for item in value:
            walk(item)

try:
    walk(json.load(sys.stdin))
except ValueError:
    pass' "${DB_INSTANCE}"
}
DATABASE_URL="$(instance_url)" || fail "could not list the Realtime Database instances of ${PROJECT_ID}"
if [[ -z "${DATABASE_URL}" ]]; then
    echo "Creating ${DB_INSTANCE} in ${DB_LOCATION}..."
    "${FIREBASE[@]}" database:instances:create "${DB_INSTANCE}" --location="${DB_LOCATION}" \
        --project="${PROJECT_ID}" --non-interactive >/dev/null
    DATABASE_URL="$(instance_url)" || fail "could not list the Realtime Database instances of ${PROJECT_ID}"
fi
[[ "${DATABASE_URL}" =~ ^https://[^[:space:],]+$ ]] || fail "could not find the URL of ${DB_INSTANCE}"

# The PROD rules deny every public read and carry the .indexOn the worker queries on every
# tick. A config naming the instance, in a scratch directory: the repository's firebase.json
# (the default instance, UAT's) is never touched.
mkdir -m 700 "${WORK}/rules"
cp database.rules.prod.json "${WORK}/rules/"
printf '{"database": [{"instance": "%s", "rules": "database.rules.prod.json"}]}\n' "${DB_INSTANCE}" \
    > "${WORK}/rules/firebase.json"
(cd "${WORK}/rules" && "${FIREBASE[@]}" deploy --only database --project="${PROJECT_ID}" \
    --non-interactive >/dev/null)

# Every node database.rules.json opens to the public must answer an anonymous read with
# "Permission denied" on this database (docs/PROD_LIVE_RUNBOOK_TH.md 2b).
PUBLIC_NODES="$(python3 -c '
import json
rules = json.load(open("database.rules.json", encoding="utf-8"))["rules"]
print(" ".join(k for k, v in rules.items() if isinstance(v, dict) and v.get(".read") is True))')"
[[ -n "${PUBLIC_NODES}" ]] || fail "could not read the public nodes of database.rules.json"
require_private() {
    local node body
    for node in ${PUBLIC_NODES}; do
        body="$(curl -sS --max-time 15 "${DATABASE_URL}/${node}.json?shallow=true")" || \
            fail "could not read ${DATABASE_URL}; check the network and run again"
        [[ "${body}" == *"Permission denied"* ]] || \
            fail "${DATABASE_URL}/${node} is readable without signing in; the PROD rules are not in place"
    done
    echo "OK database: ${DATABASE_URL} denies public reads"
}
require_private

step "3/4 Deploy lego-tick-prod: PROD, ${MODE}"
export WEBULL_ENV_OVERRIDE=PROD LEGO_MODE_OVERRIDE=observe LEGO_ACTIVE_OVERRIDE=false
export DATABASE_URL_OVERRIDE="${DATABASE_URL}"
export WEBULL_TOKEN_SECRET_OVERRIDE="projects/${PROJECT_ID}/secrets/${TOKEN_SECRET}"
export LEGO_FIX_C_OVERRIDE="${FIX_C}"
export LEGO_SYMBOL_OVERRIDE="${SYMBOL}" LEGO_DIFF_OVERRIDE="${LEGO_DIFF_OVERRIDE:-25}"
# bypass:6500 = no DNA gate until 2027-09-07, the bundle UAT runs; the script's own default
# (strategy.example.json) ended 2026-10-05 and would only ever answer PASS_DNA_EXHAUSTED.
export LEGO_DNA_BUNDLE_OVERRIDE="${LEGO_DNA_BUNDLE_OVERRIDE:-strategy.uat-continuous.json}"
export LEGO_ALLOW_FRACTIONAL_OVERRIDE="${LEGO_ALLOW_FRACTIONAL_OVERRIDE:-true}"
export LEGO_STALE_ORDER_ACTION_OVERRIDE=hold LEGO_STALE_ORDER_SECONDS_OVERRIDE=300
export LEGO_CANCEL_CONFIRM_GRACE_SECONDS_OVERRIDE=120 LEGO_SESSION_KEY_MODE_OVERRIDE=market_day
# Nothing a UAT or live shell holds travels into PROD: no authorization, no acknowledgement,
# no caps (the deploy script reads the caps from the environment only).
export LEGO_RELEASE_AUTHORIZATION_OVERRIDE='' LEGO_TRADING_WINDOW_END_OVERRIDE=''
export LEGO_MAX_ORDER_QUANTITY_OVERRIDE='' LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE=''
export LEGO_MAX_SESSION_ORDERS_OVERRIDE=''
unset LEGO_PROD_LIVE_ACK LEGO_FUNDING_MODE_OVERRIDE
if [[ "${MODE}" == preview || "${MODE}" == paper ]]; then
    # The release a trade would carry, planned for this checkout: caps proportional to
    # FIX_C, a window of 5 regular sessions, the binding (which includes the mode, so it
    # can never authorize trade). A clean environment, as in the deploy script.
    PLAN_ARGS=(--account-id-stdin --window-sessions 5 --reference-price "${PRICE}"
               --recommended-limits --enforce)
    [[ "${FUNDING}" == prefunded ]] || PLAN_ARGS+=(--initial-funding)
    PLAN_STATUS=0
    PLAN_JSON="$(gcloud secrets versions access latest --secret="${ACCOUNT_SECRET}" \
            --project="${PROJECT_ID}" | \
        env -i PATH="${PATH}" HOME="${HOME}" WEBULL_ENV=PROD LEGO_MODE="${MODE}" LEGO_ACTIVE=true \
            LEGO_SYMBOL="${LEGO_SYMBOL_OVERRIDE}" LEGO_FIX_C="${FIX_C}" LEGO_DIFF="${LEGO_DIFF_OVERRIDE}" \
            LEGO_DNA_BUNDLE="${LEGO_DNA_BUNDLE_OVERRIDE}" \
            LEGO_ALLOW_FRACTIONAL="${LEGO_ALLOW_FRACTIONAL_OVERRIDE}" \
            LEGO_STALE_ORDER_ACTION=hold LEGO_STALE_ORDER_SECONDS=300 \
            LEGO_CANCEL_CONFIRM_GRACE_SECONDS=120 LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER=1 \
            LEGO_SESSION_KEY_MODE=market_day \
            python3 ops.py release-plan "${PLAN_ARGS[@]}")" || PLAN_STATUS=$?
    [[ -n "${PLAN_JSON}" ]] || fail "release-plan produced no report (status ${PLAN_STATUS})"
    printf '%s' "${PLAN_JSON}" | python3 -c '
import json, sys
for finding in json.load(sys.stdin)["assessment"]["findings"]:
    if finding["severity"] != "INFO":
        print("  [%s] %s: %s" % (finding["severity"], finding["id"], finding["message"]))'
    (( PLAN_STATUS == 0 )) || fail "the ${MODE} release has BLOCK findings (above)"
    while IFS='=' read -r key value; do
        export "${key}=${value}"
    done < <(printf '%s' "${PLAN_JSON}" | python3 -c '
import json, re, sys
for key, value in json.load(sys.stdin)["deploy_env"].items():
    if re.fullmatch(r"[A-Z0-9_]+", key) and key != "LEGO_PROD_LIVE_ACK":
        print(f"{key}={value}")')
    [[ "${LEGO_MODE_OVERRIDE}" == "${MODE}" && "${LEGO_ACTIVE_OVERRIDE}" == true ]] || \
        fail "release-plan did not return a ${MODE} release"
    [[ "${LEGO_RELEASE_AUTHORIZATION_OVERRIDE}" =~ ^[0-9a-f]{64}$ ]] || \
        fail "release-plan returned no release authorization"
    export LEGO_FUNDING_MODE_OVERRIDE="${FUNDING}"
fi
bash deploy/cloudshell-all-in-one.sh

step "4/4 Check the database again"
# Step 8 of the deploy script deploys database.rules.json to the default instance only.
require_private
if [[ "${MODE}" == paper ]]; then
    cat <<EOF

PROD paper is running in ${DATABASE_URL}: every READY slot runs the order path up to its
Preview and books the fill on paper holdings; Place is never called. In the dashboard set the
secret FIREBASE_DB_URL_PAPER = "${DATABASE_URL}" and choose PAPER. The Webull app must show no
order. Paper fills:
  gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed" AND jsonPayload.execution.paper=true' --project=lego-firebase --freshness=1d --limit=20 --format='table(timestamp,jsonPayload.execution[0].status,jsonPayload.execution[0].paper_fill.price,jsonPayload.execution[0].paper_fill.holdings_after,jsonPayload.execution[0].preview_fee_pct)'
OPERATOR_HALT means the tripwire fired: the real account moved, or an order appeared after a
Preview. That halt lives in the paper database: check the account before preview or trade.
The release ends after 5 sessions: run this command again. Back to preview: LEGO_MODE_OVERRIDE=preview.
Stop:
  gcloud scheduler jobs pause lego-tick-prod --location=asia-southeast1 --project=lego-firebase
EOF
    exit 0
fi
if [[ "${MODE}" == preview ]]; then
    cat <<'EOF'

PROD preview is running: every READY slot runs the order path up to its Preview and stops
before Place. Watch the Webull app at the first READY slot: no order may appear. Previews and fees:
  gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed" AND jsonPayload.execution.preview_only=true' --project=lego-firebase --freshness=1d --limit=20 --format='table(timestamp,jsonPayload.execution[0].status,jsonPayload.execution[0].preview_fee_pct)'
OPERATOR_HALT means the tripwire fired: an open order or a position change right after a Preview.
The release ends after 5 sessions: run this command again. Back to observe: drop LEGO_MODE_OVERRIDE.
Stop:
  gcloud scheduler jobs pause lego-tick-prod --location=asia-southeast1 --project=lego-firebase
EOF
    exit 0
fi
cat <<'EOF'

PROD observe is running: lego-tick-prod reads the real account and quotes and sends no order.
Watch at least two regular sessions (docs/PROD_OBSERVE_TH.md):
  gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed"' --project=lego-firebase --freshness=1h --limit=10 --format='table(timestamp,jsonPayload.environment,jsonPayload.mode,jsonPayload.active,jsonPayload.business_status)'
Stop:
  gcloud scheduler jobs pause lego-tick-prod --location=asia-southeast1 --project=lego-firebase
EOF
