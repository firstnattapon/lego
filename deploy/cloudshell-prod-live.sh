#!/usr/bin/env bash
# PRODUCTION real money (trade/active) in one Cloud Shell run: the prefunded canary, or once,
# before it, the one-off funding release of a first BUY the canary caps cannot carry
# (docs/PROD_LIVE_RUNBOOK_TH.md section 16, docs/AUDIT_20261103_TH.md). From a fresh clone:
#
#   LEGO_SYMBOL_OVERRIDE=QBTS LEGO_FIX_C_OVERRIDE=1500 LEGO_DIFF_OVERRIDE=50 \
#   LEGO_LEAPS_OVERRIDE=100 LEGO_ALLOW_FRACTIONAL_OVERRIDE=true LEGO_ALERTS_CONFIRMED=yes \
#   ALERT_WEBHOOK_SECRET_OVERRIDE=<secret name> bash deploy/cloudshell-prod-live.sh
#
# The first run judges everything, prints the acknowledgement of this exact release and exits 3
# before anything is deployed; the same command with LEGO_PROD_LIVE_ACK=<that value> deploys it.
#
# LEGO_FUNDING_MODE_OVERRIDE=initial-funding (step A, once): the loose funding caps of
# release_horizon (125% of FIX_C, 2 orders per session) for 2 sessions, refused by the preflight
# unless the first order is a BUY larger than the canary caps. After its fill is confirmed and
# the holdings match the Webull app, the same command without it (step B) deploys the canary.
#
# Every gate fails closed, in this order:
#   local, before any cloud call: each money value given explicitly (no defaults), a clean
#   committed checkout, no WEBULL_API_DEBUG, LEGO_ALERTS_CONFIRMED=yes (an alert drill reached a
#   person: gate G4), a production database that is neither paper nor a default instance,
#   a funding mode that is prefunded or initial-funding, outside the session;
#   then: the PROD credentials already stored, a token Webull's Check Token calls NORMAL with more
#   than 3 days left (otherwise the 2FA login), lego-firebase-prod with rules that deny public
#   reads; the read-only account preflight (tools/prod_live_preflight.py: buying power, open
#   orders, fractional flag, position types, LEAPS held and expiry, price band, first order
#   against the caps); ops.py release-plan at the preflight's price with canary caps and a
#   5-session window (funding: the funding caps and 2 sessions) and the candidate hash of git
#   HEAD; deploy/cloudshell-all-in-one.sh, which deploys that git HEAD export only with the
#   acknowledgement.
#
# DNA and order policy are the ones deploy/cloudshell-prod-observe.sh deploys, so rolling back
# to observe (printed at the end) keeps this chain and keeps reconciling a sent order.
set -Eeuo pipefail

readonly PROJECT_ID="lego-firebase"
readonly SUFFIX="prod"
readonly DB_INSTANCE="${LEGO_PROD_DB_INSTANCE:-lego-firebase-prod}"
readonly DB_LOCATION="asia-southeast1"
readonly APP_KEY_SECRET="webull-app-key-${SUFFIX}"
readonly APP_SECRET_SECRET="webull-app-secret-${SUFFIX}"
readonly ACCOUNT_SECRET="webull-account-id-${SUFFIX}"
readonly TOKEN_SECRET="webull-token-${SUFFIX}"
readonly CANARY_WINDOW_SESSIONS=5
# The funding caps are loose: the shortest window release_horizon accepts (MIN_SESSIONS_COVERED).
readonly FUNDING_WINDOW_SESSIONS=2
readonly DNA_BUNDLE="strategy.uat-continuous.json"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

step() {
    echo
    echo "== $*"
}

is_number() {
    [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

positive() {
    is_number "$1" && awk -v v="$1" 'BEGIN { exit !(v > 0) }'
}

require_value() {
    [[ -n "$2" ]] || fail "$1 is required: a real-money release has no default for it"
}

# ----------------------------------------------------------------------------- local gates
[[ -f deploy/cloudshell-all-in-one.sh && -f database.rules.prod.json && \
   -f tools/prod_live_preflight.py && -f "${DNA_BUNDLE}" ]] || fail "run from the repository root"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || \
    fail "commit or stash local changes first: a real-money release deploys a clean, committed checkout"

SYMBOL="${LEGO_SYMBOL_OVERRIDE:-}"
FIX_C="${LEGO_FIX_C_OVERRIDE:-}"
DIFF="${LEGO_DIFF_OVERRIDE:-}"
LEAPS="${LEGO_LEAPS_OVERRIDE:-}"
FRACTIONAL="${LEGO_ALLOW_FRACTIONAL_OVERRIDE:-}"
require_value LEGO_SYMBOL_OVERRIDE "${SYMBOL}"
require_value LEGO_FIX_C_OVERRIDE "${FIX_C}"
require_value LEGO_DIFF_OVERRIDE "${DIFF}"
require_value LEGO_LEAPS_OVERRIDE "${LEAPS}"
require_value LEGO_ALLOW_FRACTIONAL_OVERRIDE "${FRACTIONAL}"
[[ "${SYMBOL}" =~ ^[A-Z][A-Z0-9.-]{0,9}$ ]] || fail "LEGO_SYMBOL_OVERRIDE must be an upper-case ticker such as QBTS"
positive "${FIX_C}" || fail "LEGO_FIX_C_OVERRIDE must be a number > 0 (USD, no comma)"
positive "${DIFF}" || fail "LEGO_DIFF_OVERRIDE must be a number > 0 (USD)"
is_number "${LEAPS}" || \
    fail "LEGO_LEAPS_OVERRIDE must be a number >= 0 (share-equivalents, 1 contract = 100)"
[[ "${FRACTIONAL}" == true || "${FRACTIONAL}" == false ]] || \
    fail "LEGO_ALLOW_FRACTIONAL_OVERRIDE must be true or false"
FEE="${LEGO_FEE_PCT_OVERRIDE:-}"
[[ -z "${FEE}" ]] || is_number "${FEE}" || \
    fail "LEGO_FEE_PCT_OVERRIDE must be a percent of notional such as 0.35"
[[ -z "${WEBULL_API_DEBUG:-}" ]] || \
    fail "unset WEBULL_API_DEBUG: on PROD the SDK would log full request and response bodies"
[[ "${LEGO_ALERTS_CONFIRMED:-}" == yes ]] || \
    fail "G4: alerts must reach a person before real money. Run python3 ops.py alert-drill --secret projects/${PROJECT_ID}/secrets/<webhook secret> --send (or drill the Cloud Monitoring policies of tools/monitoring_config.py), confirm that a person received it, then run again with LEGO_ALERTS_CONFIRMED=yes"
ALERT_SECRET="${ALERT_WEBHOOK_SECRET_OVERRIDE:-}"
[[ -z "${ALERT_SECRET}" || "${ALERT_SECRET}" =~ ^[A-Za-z0-9_-]{1,255}$ ]] || \
    fail "ALERT_WEBHOOK_SECRET_OVERRIDE must be a Secret Manager secret name, not the URL"
FUNDING="${LEGO_FUNDING_MODE_OVERRIDE:-prefunded}"
case "${FUNDING}" in
    prefunded) WINDOW_SESSIONS="${CANARY_WINDOW_SESSIONS}" ;;
    initial-funding) WINDOW_SESSIONS="${FUNDING_WINDOW_SESSIONS}" ;;
    *) fail "LEGO_FUNDING_MODE_OVERRIDE must be prefunded (the canary) or initial-funding (the one-off funding release of a first BUY larger than the canary caps)" ;;
esac
readonly FUNDING WINDOW_SESSIONS
[[ "${DB_INSTANCE}" =~ ^[a-z0-9-]+$ ]] || fail "LEGO_PROD_DB_INSTANCE must be a Realtime Database instance name"
[[ "${DB_INSTANCE}" != *paper* ]] || \
    fail "${DB_INSTANCE} holds paper holdings; real money keeps its chain in lego-firebase-prod"
[[ "${DB_INSTANCE}" != *-default-rtdb ]] || \
    fail "${DB_INSTANCE} is a default instance, the database UAT uses; name another with LEGO_PROD_DB_INSTANCE"

# The pinned requirements (the Webull SDK is the wheel in vendor/), in a venv when the system
# Python lacks them; deploy/cloudshell-all-in-one.sh uses the same python3.
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
python3 -c 'import sys; from market_clock import is_regular_session; sys.exit(1 if is_regular_session() else 0)' || \
    fail "a real-money release is refused during the regular session (09:30-16:00 New York); run it after the close"

if command -v firebase >/dev/null 2>&1; then
    FIREBASE=(firebase)
else
    command -v npx >/dev/null 2>&1 || fail "neither firebase nor npx is installed"
    FIREBASE=(npx --yes firebase-tools@latest)
fi

WORK="$(mktemp -d)"
chmod 700 "${WORK}"
trap 'rm -rf "${WORK}"' EXIT

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
    # A PROD cold start refuses interactive token creation (webull_io.build_clients): only a
    # person can approve it, in the Webull app.
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
TradeClient(api)  # creates the token, then waits until it is approved in the app
PY
    [[ -s "${dir}/token.txt" ]] || fail "Webull issued no token for this app; PROD needs one"
    python3 ops.py bootstrap-auth --token-file "${dir}/token.txt" \
        --secret "projects/${PROJECT_ID}/secrets/${TOKEN_SECRET}" >/dev/null
    add_version "${TOKEN_SECRET}" < "${dir}/token.txt"
}

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

show_findings() {  # every non-INFO finding of a report: preflight (findings) or plan (assessment)
    python3 -c '
import json, sys
report = json.load(sys.stdin)
for finding in report.get("findings") or report.get("assessment", {}).get("findings", []):
    if finding.get("severity") != "INFO":
        print("  [%s] %s: %s" % (finding.get("severity"), finding.get("id"), finding.get("message")))'
}

# ----------------------------------------------------------------------------- cloud
step "1/5 PROD credentials and token (Secret Manager)"
for secret in "${APP_KEY_SECRET}" "${APP_SECRET_SECRET}" "${ACCOUNT_SECRET}"; do
    has_version "${secret}" || \
        fail "${secret} has no ENABLED version: store the PROD credentials first (bash deploy/cloudshell-prod-observe.sh asks for them)"
done
if [[ -n "${ALERT_SECRET}" ]]; then
    has_version "${ALERT_SECRET}" || fail "alert webhook secret ${ALERT_SECRET} has no ENABLED version"
fi
ensure_container "${TOKEN_SECRET}"
if token_is_fresh; then
    echo "OK token: NORMAL, more than 3 days left"
else
    issue_token
    token_is_fresh || fail "the new token is not NORMAL with more than 3 days left; run again"
    echo "OK token: stored in ${TOKEN_SECRET}"
fi

step "2/5 PROD Realtime Database ${DB_INSTANCE} (rules deny public reads)"
"${FIREBASE[@]}" projects:list >/dev/null 2>&1 || \
    fail "Firebase CLI is not signed in; run: firebase login --no-localhost"
DATABASE_URL="$(instance_url)" || fail "could not list the Realtime Database instances of ${PROJECT_ID}"
if [[ -z "${DATABASE_URL}" ]]; then
    echo "Creating ${DB_INSTANCE} in ${DB_LOCATION}..."
    "${FIREBASE[@]}" database:instances:create "${DB_INSTANCE}" --location="${DB_LOCATION}" \
        --project="${PROJECT_ID}" --non-interactive >/dev/null
    DATABASE_URL="$(instance_url)" || fail "could not list the Realtime Database instances of ${PROJECT_ID}"
fi
[[ "${DATABASE_URL}" =~ ^https://[^[:space:],]+$ ]] || fail "could not find the URL of ${DB_INSTANCE}"
mkdir -m 700 "${WORK}/rules"
cp database.rules.prod.json "${WORK}/rules/"
printf '{"database": [{"instance": "%s", "rules": "database.rules.prod.json"}]}\n' "${DB_INSTANCE}" \
    > "${WORK}/rules/firebase.json"
(cd "${WORK}/rules" && "${FIREBASE[@]}" deploy --only database --project="${PROJECT_ID}" \
    --non-interactive >/dev/null)
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

step "3/5 Candidate (git HEAD) and the account preflight (read-only)"
CANDIDATE="$(python3 tools/candidate_manifest.py --from-head | \
    python3 -c 'import json, sys; print(json.load(sys.stdin)["candidate_hash"])')"
[[ "${CANDIDATE}" =~ ^[0-9a-f]{64}$ ]] || fail "could not compute the candidate hash of git HEAD"
echo "Candidate      : ${CANDIDATE} (git HEAD $(git rev-parse --short HEAD))"
PREFLIGHT_ARGS=(--symbol "${SYMBOL}" --fix-c "${FIX_C}" --diff "${DIFF}" --leaps "${LEAPS}"
                --allow-fractional "${FRACTIONAL}" --window-sessions "${WINDOW_SESSIONS}"
                --funding "${FUNDING}")
[[ "${LEGO_LEAPS_CONFIRMED:-}" != yes ]] || PREFLIGHT_ARGS+=(--leaps-confirmed)
mkdir -m 700 "${WORK}/preflight-token"
PREFLIGHT_STATUS=0
PREFLIGHT_JSON="$(
    key="$(gcloud secrets versions access latest --secret="${APP_KEY_SECRET}" --project="${PROJECT_ID}")"
    secret="$(gcloud secrets versions access latest --secret="${APP_SECRET_SECRET}" --project="${PROJECT_ID}")"
    account="$(gcloud secrets versions access latest --secret="${ACCOUNT_SECRET}" --project="${PROJECT_ID}")"
    WEBULL_ENV=PROD WEBULL_APP_KEY="${key}" WEBULL_APP_SECRET="${secret}" WEBULL_ACCOUNT_ID="${account}" \
        WEBULL_TOKEN_SECRET="projects/${PROJECT_ID}/secrets/${TOKEN_SECRET}" \
        WEBULL_TOKEN_DIR="${WORK}/preflight-token" WEBULL_API_DEBUG='' \
        python3 tools/prod_live_preflight.py "${PREFLIGHT_ARGS[@]}"
)" || PREFLIGHT_STATUS=$?
printf '%s' "${PREFLIGHT_JSON}" | show_findings || true
(( PREFLIGHT_STATUS == 0 )) || \
    fail "the account preflight blocks this release (above); nothing was planned or deployed"
PRICE="$(printf '%s' "${PREFLIGHT_JSON}" | python3 -c '
import json, sys
report = json.load(sys.stdin)
if report.get("ok") is not True or report.get("blocking"):
    raise SystemExit(1)
print(report["derived"]["reference_price"])')" || fail "the account preflight returned no usable report"
positive "${PRICE}" || fail "the account preflight returned no usable price"

if [[ "${FUNDING}" == initial-funding ]]; then
    step "4/5 Plan the release: one-off FUNDING caps, ${WINDOW_SESSIONS} sessions, the price ${PRICE}"
else
    step "4/5 Plan the release: canary caps, ${WINDOW_SESSIONS} sessions, the price ${PRICE}"
fi
PLAN_ARGS=(--account-id-stdin --candidate-hash "${CANDIDATE}" --window-sessions "${WINDOW_SESSIONS}"
           --reference-price "${PRICE}" --recommended-limits --enforce)
[[ "${FUNDING}" != initial-funding ]] || PLAN_ARGS+=(--initial-funding)
[[ -z "${FEE}" ]] || PLAN_ARGS+=(--fee-pct "${FEE}")
PLAN_STATUS=0
PLAN_JSON="$(gcloud secrets versions access latest --secret="${ACCOUNT_SECRET}" --project="${PROJECT_ID}" | \
    env -i PATH="${PATH}" HOME="${HOME}" WEBULL_ENV=PROD LEGO_MODE=trade LEGO_ACTIVE=true \
        LEGO_SYMBOL="${SYMBOL}" LEGO_FIX_C="${FIX_C}" LEGO_DIFF="${DIFF}" LEGO_LEAPS="${LEAPS}" \
        LEGO_DNA_BUNDLE="${DNA_BUNDLE}" LEGO_ALLOW_FRACTIONAL="${FRACTIONAL}" \
        LEGO_STALE_ORDER_ACTION=hold LEGO_STALE_ORDER_SECONDS=300 \
        LEGO_CANCEL_CONFIRM_GRACE_SECONDS=120 LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER=1 \
        LEGO_SESSION_KEY_MODE=market_day ALERT_WEBHOOK_SECRET_OVERRIDE="${ALERT_SECRET}" \
        python3 ops.py release-plan "${PLAN_ARGS[@]}")" || PLAN_STATUS=$?
[[ -n "${PLAN_JSON}" ]] || fail "release-plan produced no report (status ${PLAN_STATUS})"
printf '%s' "${PLAN_JSON}" | show_findings || true
(( PLAN_STATUS == 0 )) || fail "the release has BLOCK findings (above); nothing was deployed"
unset LEGO_FUNDING_MODE_OVERRIDE   # set again below only if the plan is the funding release
while IFS='=' read -r key value; do
    export "${key}=${value}"
done < <(printf '%s' "${PLAN_JSON}" | python3 -c '
import json, re, sys
for key, value in json.load(sys.stdin)["deploy_env"].items():
    if re.fullmatch(r"[A-Z0-9_]+", key) and key != "LEGO_PROD_LIVE_ACK":
        print(f"{key}={value}")')
[[ "${WEBULL_ENV_OVERRIDE:-}" == PROD && "${LEGO_MODE_OVERRIDE:-}" == trade && \
   "${LEGO_ACTIVE_OVERRIDE:-}" == true ]] || fail "release-plan did not return a PROD trade release"
[[ "${LEGO_RELEASE_AUTHORIZATION_OVERRIDE:-}" =~ ^[0-9a-f]{64}$ ]] || \
    fail "release-plan returned no release authorization"
[[ "${EXPECTED_CANDIDATE_HASH:-}" == "${CANDIDATE}" ]] || fail "release-plan planned another candidate"
[[ "${LEGO_FUNDING_MODE_OVERRIDE:-prefunded}" == "${FUNDING}" ]] || \
    fail "release-plan planned another funding mode than ${FUNDING}"
export DATABASE_URL_OVERRIDE="${DATABASE_URL}"
export WEBULL_TOKEN_SECRET_OVERRIDE="projects/${PROJECT_ID}/secrets/${TOKEN_SECRET}"
export LEGO_FUNDING_MODE_OVERRIDE="${FUNDING}"
export ALERT_WEBHOOK_SECRET_OVERRIDE="${ALERT_SECRET}"
export LEGO_FEE_PCT_OVERRIDE="${FEE}"
unset LEGO_SCHEDULE_OVERRIDE   # every minute, as tools/verify_deployment.py requires

print_summary() {
    printf '%s' "${PREFLIGHT_JSON}" | python3 -c '
import json, sys
derived = json.load(sys.stdin)["derived"]
plan = json.loads(sys.argv[1])["release"]
funding, confirmed = sys.argv[7], sys.argv[8] == "yes"
first = derived.get("first_order")
band = derived.get("price_band") or {}
if sys.argv[5] in ("0", "0.0"):
    leaps_source = "none"
elif confirmed and derived.get("leaps_held") in (None, "0"):
    leaps_source = "owner-confirmed (LEGO_LEAPS_CONFIRMED=yes), NOT seen in the positions"
else:
    leaps_source = "verified in the positions (%s held)" % derived.get("leaps_held")
rows = [
    ("Symbol / FIX_C / DIFF / LEAPS / fractional", " / ".join(sys.argv[2:7])),
    ("LEAPS", leaps_source),
    ("Last price (reference)", "%s (quote %s)" % (derived.get("reference_price"),
                                                   derived.get("quote_time") or "time not given")),
    ("Real shares / LEAPS held / real target", "%s / %s / %s" % (
        derived.get("real_shares"), derived.get("leaps_held"), derived.get("real_target"))),
    ("First order", "none: within DIFF of FIX_C" if not first else
     "%s about %s USD (%s shares)" % (first["side"], first["notional_usd"], first["quantity"])),
    ("Caps (quantity / notional / orders)", "%s / %s USD / %s per session" % (
        plan["LEGO_MAX_ORDER_QUANTITY"], plan["LEGO_MAX_ORDER_NOTIONAL_USD"],
        plan["LEGO_MAX_SESSION_ORDERS"])),
    ("Window ends", plan["LEGO_TRADING_WINDOW_END"]),
    ("Buying power / required", "%s / %s USD" % (
        derived.get("buying_power_usd"), derived.get("required_cash_usd"))),
    ("LEAPS price band", "BUY below %s, SELL below %s" % (band.get("buy_below"), band.get("sell_below"))
     if band else ("no LEAPS" if sys.argv[5] in ("0", "0.0") else "see the release plan above")),
]
print()
if funding == "initial-funding":
    print("REAL-MONEY RELEASE (one-off FUNDING: caps up to %s USD per order; expect one BUY of about %s USD)"
          % (plan["LEGO_MAX_ORDER_NOTIONAL_USD"], first["notional_usd"] if first else "?"))
else:
    print("REAL-MONEY RELEASE (prefunded canary)")
for label, value in rows:
    print("  %-42s: %s" % (label, value))' "${PLAN_JSON}" "${SYMBOL}" "${FIX_C}" "${DIFF}" "${LEAPS}" "${FRACTIONAL}" \
        "${FUNDING}" "${LEGO_LEAPS_CONFIRMED:-}"
}

print_funding_next_step() {
    [[ "${FUNDING}" == initial-funding ]] || return 0
    cat <<'EOF'

THIS IS THE ONE-OFF FUNDING RELEASE: its caps are loose on purpose, keep it short.
  * Watch its first BUY fill (python3 ops.py status: nothing in flight; holdings = the Webull app).
  * The same evening, after the close: run this command again WITHOUT LEGO_FUNDING_MODE_OVERRIDE
    to deploy the prefunded canary (its caps replace these). Do not leave this release running.
  * A fill that is not confirmed within one tick (AWAITING_FILL_CONFIRMATION): the kill switch below.
EOF
}

print_stop_commands() {
    local halt="WEBULL_ENV=PROD LEGO_SYMBOL=${SYMBOL} LEGO_FIX_C=${FIX_C} LEGO_DIFF=${DIFF} FIREBASE_DB_URL=${DATABASE_URL} WEBULL_ACCOUNT_ID=\"\$(gcloud secrets versions access latest --secret=${ACCOUNT_SECRET} --project=${PROJECT_ID})\" python3 ops.py halt-orders --operator \"<your name>\" --reason \"<why>\""
    cat <<EOF

Stop new orders at once (kill switch: no redeploy; an order already sent is still reconciled).
Dry run, then the same with --apply:
  ${halt}
  ${halt} --apply
Roll back to observe on the same chain (no new orders; a sent order is still reconciled):
  LEGO_ALLOW_FRACTIONAL_OVERRIDE=${FRACTIONAL} LEGO_SYMBOL_OVERRIDE=${SYMBOL} LEGO_FIX_C_OVERRIDE=${FIX_C} LEGO_DIFF_OVERRIDE=${DIFF} LEGO_LEAPS_OVERRIDE=${LEAPS} bash deploy/cloudshell-prod-observe.sh
EOF
}

step "5/5 Deploy lego-tick-prod: PROD, trade, active (deploy/cloudshell-all-in-one.sh)"
DEPLOY_STATUS=0
bash deploy/cloudshell-all-in-one.sh || DEPLOY_STATUS=$?
if (( DEPLOY_STATUS == 3 )); then
    print_summary
    print_funding_next_step
    print_stop_commands
    cat <<'EOF'

Nothing was deployed. Read every value above. To deploy exactly this release, run the same
command again with LEGO_PROD_LIVE_ACK=<the value printed above>, outside the regular session.
EOF
    exit 3
fi
if (( DEPLOY_STATUS != 0 )); then
    echo "ERROR: deploy/cloudshell-all-in-one.sh failed (status ${DEPLOY_STATUS})" >&2
    exit "${DEPLOY_STATUS}"
fi

require_private
print_summary
print_funding_next_step
print_stop_commands
cat <<EOF

PROD trade is deployed. Watch the first session from 09:30 New York (orders, fills, holdings):
  gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lego-tick-prod" AND jsonPayload.event="lego_tick_completed"' --project=${PROJECT_ID} --freshness=1h --limit=20 --format='table(timestamp,jsonPayload.business_status,jsonPayload.decision.status,jsonPayload.decision.real_holdings,jsonPayload.execution[0].status)'
Stop at once (kill switch above) on: needs_manual_check, order_contract_anomaly, BROKER_REJECT_HALT,
MANUAL_RECONCILIATION_REQUIRED, AWAITING_FILL_CONFIRMATION past one tick, AWAITING_BROKER_FEE past
15 minutes, FEE_OVERDUE, EXECUTION_LIMIT_BLOCKED or NOT_PLACED again and again, holdings that differ
from the Webull app, or any order in the app this system did not send (docs/PROD_LIVE_RUNBOOK_TH.md 16).
The release ends after ${WINDOW_SESSIONS} sessions: run this command again before then.
EOF
