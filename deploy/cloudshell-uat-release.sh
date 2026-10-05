#!/usr/bin/env bash
# LEGO UAT release, Google Cloud Shell, all in one.
#
#   curl -fsSL <raw url of this file> -o ~/uat-release.sh && bash ~/uat-release.sh
#
# Clones the reviewed `main` into a fresh folder, plans the release (approval window,
# DNA, caps, release binding) and deploys it with deploy/continuous-uat.sh:
#   UAT / trade / active / UBER / FIX_C=10000 / fractional=true
# UAT only. Nothing here touches production, and nothing renews itself: run it again
# before the window it prints ends (a RELEASE_EXPIRING alert fires 48 hours ahead).
#
# Optional settings (export before running):
#   WINDOW_SESSIONS=10                      approval window = close of the Nth regular session
#   ALERT_WEBHOOK_SECRET_OVERRIDE=<secret>  Secret Manager secret holding the alert URL
#   ASSUME_YES=1                            skip the DEPLOY confirmation
set -Eeuo pipefail
STEP=0
trap 'echo "stopped at step ${STEP}/4 (line ${LINENO}); $( (( STEP < 4 )) && echo "nothing was deployed" || echo "read the error above; the deploy may be partial and the script is safe to re-run")" >&2' ERR

REPO_URL="${REPO_URL:-https://github.com/firstnattapon/lego.git}"
REPO_REF="${REPO_REF:-main}"
WINDOW_SESSIONS="${WINDOW_SESSIONS:-10}"
WORKROOT="${WORKROOT:-${HOME}/lego-release-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
DEPS_DIR="${DEPS_DIR:-${HOME}/.lego-deploy-deps}"
readonly PROJECT_ID="lego-firebase"                 # the project deploy/cloudshell-all-in-one.sh uses
readonly ACCOUNT_SECRET="webull-account-id-uat"

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

for tool in gcloud git python3; do
    command -v "${tool}" >/dev/null 2>&1 || fail "missing command: ${tool}"
done
[[ "${WINDOW_SESSIONS}" =~ ^[0-9]+$ ]] && (( WINDOW_SESSIONS >= 2 )) || \
    fail "WINDOW_SESSIONS must be an integer >= 2"

STEP=1; say "1/4 fetch source (${REPO_REF}) into a fresh folder"
mkdir -p "${WORKROOT}"
git clone --quiet --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${WORKROOT}/lego"
cd "${WORKROOT}/lego"
git --no-pager log -1 --format='  commit  %H%n  author  %an%n  date    %ad%n  subject %s' --date=iso

# The local checks import dna_engine (numpy) and market_clock (tzdata). Install exactly
# the versions requirements.txt pins into a private folder and put a python3 shim first
# on PATH (deploy/cloudshell-all-in-one.sh re-runs python3 under `env -i`, which drops
# PYTHONPATH, but keeps PATH). No venv and no pip inside a venv: Cloud Shell's venv can
# come without pip. The system Python is not modified. The Cloud Function installs its
# own dependencies at build time.
STEP=2; say "2/4 prepare Python"
if PYTHONPATH="${DEPS_DIR}" python3 -c 'import numpy, tzdata' 2>/dev/null; then
    echo "  dependencies already in ${DEPS_DIR}"
elif python3 -c 'import numpy, tzdata' 2>/dev/null; then
    echo "  using the numpy and tzdata already installed"
    DEPS_DIR=""
else
    python3 -m pip --version >/dev/null 2>&1 || fail "pip not found: run  sudo apt-get install -y python3-pip  and re-run"
    python3 -m pip install --quiet --disable-pip-version-check --target "${DEPS_DIR}" \
        "$(grep -E '^numpy==' requirements.txt)" "$(grep -E '^tzdata==' requirements.txt)"
fi
if [[ -n "${DEPS_DIR}" ]]; then
    REAL_PYTHON="$(command -v python3)"
    mkdir -p "${DEPS_DIR}/bin"
    cat > "${DEPS_DIR}/bin/python3" <<SHIM
#!/bin/sh
PYTHONPATH="${DEPS_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}" exec "${REAL_PYTHON}" "\$@"
SHIM
    chmod +x "${DEPS_DIR}/bin/python3"
    export PATH="${DEPS_DIR}/bin:${PATH}"
fi
python3 -c 'import numpy, tzdata' || fail "numpy/tzdata still not importable"

STEP=3; say "3/4 plan the release (window ${WINDOW_SESSIONS} sessions, DNA, caps): read-only, deploys nothing"
PLAN_STATUS=0
PLAN="$(
    gcloud secrets versions access latest --secret="${ACCOUNT_SECRET}" --project="${PROJECT_ID}" |
        python3 ops.py release-plan --env-file deploy/uat-continuous.env.example --account-id-stdin \
            --window-sessions "${WINDOW_SESSIONS}" --enforce
)" || PLAN_STATUS=$?
[[ -n "${PLAN}" ]] || fail "release-plan produced nothing: check that secret ${ACCOUNT_SECRET} exists in ${PROJECT_ID} and you may read it (first time: run  bash deploy/cloudshell-all-in-one.sh  once to create the secrets)"
printf '%s' "${PLAN}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
r, a = d["release"], d["assessment"]
print("  environment : %s / %s (active=%s)  symbol=%s  principal=%s USD  fractional=%s" % (
    d["environment"], d["mode"], d["active"], d["symbol"], d["principal_usd"], d["allow_fractional"]))
print("  candidate   : %s (%s)" % (d["candidate_hash"], d["candidate_scope"]))
print("  window ends : %s UTC  (%s complete sessions)" % (r["LEGO_TRADING_WINDOW_END"], a["limits"]["complete_sessions_in_window"]))
print("  caps        : qty %s | notional %s USD | %s orders per session" % (
    r["LEGO_MAX_ORDER_QUANTITY"], r["LEGO_MAX_ORDER_NOTIONAL_USD"], r["LEGO_MAX_SESSION_ORDERS"]))
print("  DNA         : %s slots, ends %s" % (a["dna"]["length"], a["dna"]["end_utc"]))
for finding in a["findings"]:
    if finding["severity"] != "INFO":
        print("  [%s] %s: %s" % (finding["severity"], finding["id"], finding["message"]))
'
(( PLAN_STATUS == 0 )) || fail "this release failed the checks (BLOCK above), nothing deployed"

plan_value() {
    printf '%s' "${PLAN}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["deploy_env"][sys.argv[1]])' "$1"
}
export EXPECTED_CANDIDATE_HASH="$(plan_value EXPECTED_CANDIDATE_HASH)"
export LEGO_RELEASE_AUTHORIZATION_OVERRIDE="$(plan_value LEGO_RELEASE_AUTHORIZATION_OVERRIDE)"
export LEGO_TRADING_WINDOW_END_OVERRIDE="$(plan_value LEGO_TRADING_WINDOW_END_OVERRIDE)"

if [[ "${ASSUME_YES:-0}" != "1" ]]; then
    echo
    echo "Next step acts on project ${PROJECT_ID} (UAT, test account):"
    echo "  - creates/updates Cloud Function lego-tick-uat and its Cloud Scheduler job (every minute)"
    echo "  - the final smoke tick may send one UAT order if the market is open (09:30-16:00 New York)"
    read -r -p "type DEPLOY to continue: " answer </dev/tty
    [[ "${answer}" == "DEPLOY" ]] || fail "cancelled, nothing deployed"
fi

STEP=4; say "4/4 deploy (deploy/continuous-uat.sh)"
bash deploy/continuous-uat.sh

say "done, next"
echo "  - renew before ${LEGO_TRADING_WINDOW_END_OVERRIDE} (re-run this script) or orders are blocked"
echo "  - set ALERT_WEBHOOK_SECRET_OVERRIDE and apply tools/monitoring_config.py so alerts reach a person"
echo "  - first tick with the market open: business_status RELEASE_UNAUTHORIZED means the deploy does not match the release"
