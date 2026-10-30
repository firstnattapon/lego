"""Deploy scripts: the production acknowledgement flow, preflight and alert wiring.

cloudshell-all-in-one.sh is run for real in a throwaway git checkout with stub
gcloud/firebase binaries on PATH, so the guards, the acknowledgement flow and the
horizon preflight are exercised without any cloud access. Nothing here touches a
network, a broker or Secret Manager.
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ops
import release_horizon as rh
from config import release_binding_for
from tools.make_dna_bundle import build_bundle

ROOT = Path(__file__).parent
ORIGIN = "2026-09-08T13:30:00Z"
ACCOUNT = "test-account-id"
COPIED = ["config.py", "dna_engine.py", "recovery_policy.py", "execution_limits.py",
          "release_horizon.py", "operational_health.py", "market_clock.py", "ops.py",
          "firebase.json", "database.rules.json", ".gitignore", "tools/candidate_manifest.py",
          "requirements.txt", "requirements-dev.txt", "deploy/cloudshell-all-in-one.sh",
          "deploy/cloudshell-prod-observe.sh", "database.rules.prod.json"]

GCLOUD = r"""#!/usr/bin/env bash
echo "gcloud $*" >> "${STUB_LOG}"
case "$*" in
  "auth list"*) echo "tester@example.com" ;;
  "projects describe"*projectNumber*) echo "123456789" ;;
  "projects describe"*|"config set"*|"services enable"*) ;;
  "iam service-accounts describe"*|"secrets describe"*) ;;
  "secrets versions describe"*) echo "ENABLED" ;;
  "secrets versions access"*webull-token-prod*) printf 'stub-token\n%s000\nNORMAL\n' "$(( $(date +%s) + 864000 ))" ;;
  "secrets versions access"*) printf '%s' "test-account-id" ;;
  "secrets add-iam-policy-binding"*|"projects add-iam-policy-binding"*) ;;
  "functions describe"*|"scheduler jobs describe"*) exit 1 ;;
  "functions deploy"*) echo "GCLOUD_FUNCTIONS_DEPLOY_REACHED" >> "${STUB_LOG}"; exit 78 ;;
  *) echo "unexpected gcloud call: $*" >&2; exit 99 ;;
esac
"""
FIREBASE = r"""#!/usr/bin/env bash
case "$1" in
  projects:list) echo '[{"projectId": "lego-firebase"}]' ;;
  database:instances:list) [[ -n "${STUB_RTDB_INSTANCES:-}" ]] || exit 99; echo "${STUB_RTDB_INSTANCES}" ;;
  deploy) echo "FIREBASE_DEPLOY_REACHED $* $(tr -d ' \n' < firebase.json)" >> "${STUB_LOG}" ;;
  *) echo "unexpected firebase call: $*" >&2; exit 99 ;;
esac
"""
CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "${STUB_LOG}"
if [[ -n "${STUB_RTDB_BODY:-}" ]]; then echo "${STUB_RTDB_BODY}"; else echo '{"error" : "Permission denied"}'; fi
"""
# Stands in for Webull's Check Token (`ops.py token-status`) and, on request, for the
# 2FA script of issue_token; every other python3 call is the real interpreter, named
# here because the scripts also run python3 under `env -i`.
PYTHON3 = r"""#!/usr/bin/env bash
if [[ "${1:-}" == ops.py && "${2:-}" == token-status ]]; then
  cat > /dev/null
  echo "python3 ops.py token-status WEBULL_ENV=${WEBULL_ENV:-} app_key=${WEBULL_APP_KEY:+set}" >> "${STUB_LOG}"
  case "${STUB_TOKEN_STATUS:-unreachable}" in
    fresh) echo '{"status": "NORMAL", "live_expires_at": "2026-12-01T00:00:00Z", "days_left": 30.0, "fresh": true, "exit_code": 0}'; exit 0 ;;
    rejected) echo '{"status": "INVALID", "live_expires_at": null, "days_left": null, "fresh": false, "exit_code": 1}'; exit 1 ;;
    *) echo '{"error": "Check Token unavailable: ConnectionError", "exit_code": 2}'; exit 2 ;;
  esac
fi
if [[ "${1:-}" == - && -n "${STUB_ISSUE_TOKEN_FAILS:-}" ]]; then
  cat > /dev/null
  echo "python3 - (issue_token)" >> "${STUB_LOG}"
  exit 1
fi
exec "@REAL_PYTHON@" "$@"
""".replace("@REAL_PYTHON@", sys.executable)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deploy-repo")
    stubs = tmp_path_factory.mktemp("stubs")
    for name in COPIED:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, repo / name)
    # The real calendar decides "regular session"; the tests decide it with an env var.
    with (repo / "market_clock.py").open("a") as handle:
        handle.write('\n\ndef is_regular_session(at=None):  # test override\n'
                     '    return os.environ.get("FAKE_SESSION") == "1"\n')
    # A DNA that outlives any clock the tests run under.
    (repo / "strategy.long.json").write_text(
        json.dumps(build_bundle("bypass:20000", ORIGIN, 900), indent=2) + "\n")
    for name, body in (("gcloud", GCLOUD), ("firebase", FIREBASE), ("curl", CURL),
                       ("python3", PYTHON3)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "sandbox"], cwd=repo, check=True)
    manifest = subprocess.run([sys.executable, "tools/candidate_manifest.py"], cwd=repo,
                              check=True, capture_output=True, text=True)
    return {"repo": repo, "stubs": stubs, "log": stubs / "stub.log",
            "candidate": json.loads(manifest.stdout)["candidate_hash"]}


def release_env(**overrides):
    window = rh.format_window_end(rh.window_end_after_sessions(datetime.now(timezone.utc), 5))
    env = {
        "WEBULL_ENV_OVERRIDE": "PROD", "LEGO_MODE_OVERRIDE": "trade", "LEGO_ACTIVE_OVERRIDE": "true",
        "LEGO_SYMBOL_OVERRIDE": "UBER", "LEGO_FIX_C_OVERRIDE": "10000", "LEGO_DIFF_OVERRIDE": "25",
        "LEGO_ALLOW_FRACTIONAL_OVERRIDE": "false", "LEGO_DNA_BUNDLE_OVERRIDE": "strategy.long.json",
        "LEGO_SESSION_KEY_MODE_OVERRIDE": "market_day", "LEGO_STALE_ORDER_ACTION_OVERRIDE": "hold",
        "LEGO_STALE_ORDER_SECONDS_OVERRIDE": "300", "LEGO_CANCEL_CONFIRM_GRACE_SECONDS_OVERRIDE": "120",
        "LEGO_MAX_ORDER_QUANTITY_OVERRIDE": "20", "LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE": "1000",
        "LEGO_MAX_SESSION_ORDERS_OVERRIDE": "10", "LEGO_TRADING_WINDOW_END_OVERRIDE": window,
        "WEBULL_TOKEN_SECRET_OVERRIDE": "projects/lego-firebase/secrets/webull-token-prod"}
    env.update(overrides)
    return env


def authorized(sandbox, env):
    """Add the candidate and the release authorization the script will demand."""
    runtime = {name: env[override] for name, override in ops.DEPLOY_OVERRIDES.items()
               if override in env}
    runtime.update(LEGO_CANDIDATE_HASH=sandbox["candidate"], WEBULL_ACCOUNT_ID=ACCOUNT,
                   LEGO_DNA_BUNDLE=str(sandbox["repo"] / env["LEGO_DNA_BUNDLE_OVERRIDE"]))
    return {**env, "EXPECTED_CANDIDATE_HASH": sandbox["candidate"],
            "LEGO_RELEASE_AUTHORIZATION_OVERRIDE": release_binding_for(runtime)}


def run(sandbox, env, **extra):
    sandbox["log"].write_text("")
    process_env = {
        "PATH": f"{sandbox['stubs']}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(sandbox["repo"]), "STUB_LOG": str(sandbox["log"]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL_OVERRIDE": "https://example-default-rtdb.firebaseio.com", **env, **extra}
    return subprocess.run(["bash", "deploy/cloudshell-all-in-one.sh"], cwd=sandbox["repo"],
                          env=process_env, capture_output=True, text=True, timeout=180)


def log(sandbox):
    return sandbox["log"].read_text()


# ------------------------------------------------------------------- static checks
@pytest.mark.parametrize("script", sorted(p.name for p in (ROOT / "deploy").glob("*.sh")))
def test_every_shell_script_parses(script):
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / script)]).returncode == 0


def test_continuous_uat_script_agrees_with_the_policy_file():
    """release-plan reads the policy file; the script deploys its own defaults."""
    script = (ROOT / "deploy" / "continuous-uat.sh").read_text()
    policy = ops._read_env_file(ROOT / "deploy" / "uat-continuous.env.example")

    def exported(name):
        return re.search(rf"\b{name}_OVERRIDE=(\S+)", script).group(1)

    def defaulted(name):
        return re.search(rf'{name}_OVERRIDE="\$\{{{name}_OVERRIDE:-([^}}]*)\}}"', script).group(1)

    for name in ("WEBULL_ENV", "LEGO_MODE", "LEGO_ACTIVE", "LEGO_SYMBOL", "LEGO_FIX_C", "LEGO_DIFF",
                 "LEGO_SESSION_KEY_MODE", "LEGO_STALE_ORDER_ACTION",
                 "LEGO_STALE_ORDER_SECONDS", "LEGO_CANCEL_CONFIRM_GRACE_SECONDS"):
        assert exported(name) == policy[name], name
    # LEGO_ALLOW_FRACTIONAL moved here 2026-10-20: the whole-share control run needs it overridable.
    for name in ("LEGO_DNA_BUNDLE", "LEGO_MAX_SESSION_ORDERS", "LEGO_MAX_ORDER_QUANTITY",
                 "LEGO_MAX_ORDER_NOTIONAL_USD", "LEGO_ALLOW_FRACTIONAL"):
        assert defaulted(name) == policy[name], name


def test_powershell_deploy_no_longer_accepts_production_live():
    text = (ROOT / "deploy" / "deploy.ps1").read_text()
    assert "$Environment -eq 'PROD' -and ($Mode -ne 'observe' -or $Active)" in text
    assert "cloudshell-all-in-one.sh with LEGO_PROD_LIVE_ACK" in text


def test_the_production_template_stays_observe_only():
    text = (ROOT / "deploy" / "cloudrun_prod_template.yaml").read_text()
    assert "LEGO_MODE" in text and "value: observe" in text and "value: 'false'" in text


# ------------------------------------------------------ the production guard (step 0)
@pytest.mark.parametrize("mode,active", [("trade", "false"), ("observe", "true")])
def test_production_half_states_are_refused(sandbox, mode, active):
    result = run(sandbox, release_env(LEGO_MODE_OVERRIDE=mode, LEGO_ACTIVE_OVERRIDE=active))
    assert result.returncode == 1
    assert "must be observe/inactive, or trade/active" in result.stderr
    assert log(sandbox) == ""                                     # nothing was called


def test_production_live_is_refused_during_the_regular_session(sandbox):
    result = run(sandbox, release_env(), FAKE_SESSION="1")
    assert result.returncode == 1
    assert "refused during the regular session" in result.stderr
    assert log(sandbox) == ""


def test_an_expired_window_is_refused_before_any_cloud_call(sandbox):
    result = run(sandbox, release_env(WEBULL_ENV_OVERRIDE="UAT",
                                      LEGO_TRADING_WINDOW_END_OVERRIDE="2000-01-01T00:00:00Z"))
    assert result.returncode != 0 and "trading window expired" in result.stderr
    assert log(sandbox) == ""


# ----------------------------------------------------- the acknowledgement flow (7)
def test_production_live_prints_the_acknowledgement_and_stops_before_deploy(sandbox):
    result = run(sandbox, authorized(sandbox, release_env()))
    assert result.returncode == 3, result.stderr
    assert "REAL-MONEY RELEASE" in result.stdout
    assert re.search(r"LEGO_PROD_LIVE_ACK=LIVE-PROD-UBER-q20-n1000-o10-\d{8}T\d{4}Z-prefunded-[0-9a-f]{16}",
                     result.stdout)
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)
    assert "GCLOUD_FUNCTIONS_DEPLOY_REACHED" not in log(sandbox)


def test_a_wrong_acknowledgement_is_refused(sandbox):
    result = run(sandbox, authorized(sandbox, release_env()),
                 LEGO_PROD_LIVE_ACK="LIVE-PROD-UBER-q20-n1000-o10-20990101T2000Z-prefunded-0123456789abcdef")
    assert result.returncode == 1 and "does not match this release" in result.stderr
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


def test_the_matching_acknowledgement_reaches_deploy_and_travels_to_the_function(sandbox):
    env = authorized(sandbox, release_env())
    ack = re.search(r"LEGO_PROD_LIVE_ACK=(\S+)", run(sandbox, env).stdout).group(1)
    result = run(sandbox, env, LEGO_PROD_LIVE_ACK=ack)
    assert result.returncode == 78, result.stderr              # the stub gcloud stops at `functions deploy`
    assert "PROD live acknowledgement verified" in result.stdout
    recorded = log(sandbox)
    assert "FIREBASE_DEPLOY_REACHED" in recorded and "GCLOUD_FUNCTIONS_DEPLOY_REACHED" in recorded
    assert f"LEGO_PROD_LIVE_ACK={ack}" in recorded and "WEBULL_ENV=PROD" in recorded


def test_a_production_release_that_cannot_be_acknowledged_is_refused(sandbox):
    env = authorized(sandbox, release_env(LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE="3000"))   # 30% of principal
    result = run(sandbox, env)
    assert result.returncode == 1
    assert "[BLOCK] notional_cap_ratio" in result.stdout
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


FUNDING = {"LEGO_MAX_ORDER_QUANTITY_OVERRIDE": "200", "LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE": "12500",
           "LEGO_MAX_SESSION_ORDERS_OVERRIDE": "2"}      # 125% of principal: the one-off funding release


def test_funding_caps_are_refused_unless_the_funding_release_is_requested(sandbox):
    result = run(sandbox, authorized(sandbox, release_env(**FUNDING)))
    assert result.returncode == 1
    assert "[BLOCK] notional_cap_ratio" in result.stdout
    assert "LEGO_FUNDING_MODE_OVERRIDE=initial-funding" in result.stderr        # tells the operator how
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


def test_the_funding_release_deploys_with_its_own_acknowledgement(sandbox):
    env = authorized(sandbox, release_env(LEGO_FUNDING_MODE_OVERRIDE="initial-funding", **FUNDING))
    first = run(sandbox, env)
    assert first.returncode == 3, first.stdout + first.stderr
    ack = re.search(r"LEGO_PROD_LIVE_ACK=(\S+)", first.stdout).group(1)
    assert re.fullmatch(r"LIVE-PROD-UBER-q200-n12500-o2-\d{8}T\d{4}Z-initial-funding-[0-9a-f]{16}", ack)
    assert "funding_release_temporary" in first.stdout                         # loose caps are called out
    result = run(sandbox, env, LEGO_PROD_LIVE_ACK=ack)
    assert result.returncode == 78, result.stdout + result.stderr
    recorded = log(sandbox)
    assert f"LEGO_PROD_LIVE_ACK={ack}" in recorded and "GCLOUD_FUNCTIONS_DEPLOY_REACHED" in recorded


def test_the_funding_mode_does_not_loosen_a_steady_release(sandbox):
    """Asking for the funding assessment cannot launder caps that are not funding caps."""
    env = authorized(sandbox, release_env(LEGO_FUNDING_MODE_OVERRIDE="initial-funding"))   # 1000 / 20 / 10
    result = run(sandbox, env)
    assert result.returncode == 1
    assert "[BLOCK] funding_notional_sufficient" in result.stdout
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


def test_an_unknown_funding_mode_is_refused_before_any_cloud_call(sandbox):
    result = run(sandbox, authorized(sandbox, release_env(LEGO_FUNDING_MODE_OVERRIDE="yes")))
    assert result.returncode == 1 and "LEGO_FUNDING_MODE_OVERRIDE must be" in result.stderr
    assert log(sandbox) == ""


def test_production_observe_deploys_without_an_acknowledgement(sandbox):
    env = release_env(LEGO_MODE_OVERRIDE="observe", LEGO_ACTIVE_OVERRIDE="false")
    for name in ("LEGO_MAX_ORDER_QUANTITY_OVERRIDE", "LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE",
                 "LEGO_MAX_SESSION_ORDERS_OVERRIDE", "LEGO_TRADING_WINDOW_END_OVERRIDE"):
        env.pop(name)
    result = run(sandbox, env)
    assert result.returncode == 78, result.stderr
    assert "LEGO_PROD_LIVE_ACK" not in log(sandbox) and "release horizon" not in result.stderr


# ------------------------------------------------- the horizon preflight (UAT, too)
def test_uat_trade_with_a_window_of_one_session_is_refused_before_deploy(sandbox):
    one = rh.format_window_end(rh.window_end_after_sessions(datetime.now(timezone.utc), 1))
    env = authorized(sandbox, release_env(WEBULL_ENV_OVERRIDE="UAT", LEGO_ALLOW_FRACTIONAL_OVERRIDE="true",
                                          LEGO_TRADING_WINDOW_END_OVERRIDE=one))
    result = run(sandbox, env)
    assert result.returncode == 1
    assert "[BLOCK] window_sessions" in result.stdout and "check failed" in result.stderr
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


def test_uat_trade_with_a_valid_plan_reaches_deploy(sandbox):
    env = authorized(sandbox, release_env(
        WEBULL_ENV_OVERRIDE="UAT", LEGO_ALLOW_FRACTIONAL_OVERRIDE="true",
        LEGO_MAX_ORDER_QUANTITY_OVERRIDE="30", LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE="1500",
        LEGO_MAX_SESSION_ORDERS_OVERRIDE="26"))
    result = run(sandbox, env)
    assert result.returncode == 78, result.stderr
    assert "LEGO_PROD_LIVE_ACK" not in log(sandbox)               # UAT never carries one


# ------------------------------------------------------------------ alert webhook
def observe_env(**extra):
    env = release_env(WEBULL_ENV_OVERRIDE="UAT", LEGO_MODE_OVERRIDE="observe",
                      LEGO_ACTIVE_OVERRIDE="false", **extra)
    for name in ("LEGO_MAX_ORDER_QUANTITY_OVERRIDE", "LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE",
                 "LEGO_MAX_SESSION_ORDERS_OVERRIDE", "LEGO_TRADING_WINDOW_END_OVERRIDE"):
        env.pop(name)
    return env


def test_the_alert_webhook_is_a_secret_never_an_environment_value(sandbox):
    result = run(sandbox, observe_env(), ALERT_WEBHOOK_SECRET_OVERRIDE="lego-alert-webhook-uat")
    assert result.returncode == 78, result.stderr
    recorded = log(sandbox)
    assert "ALERT_WEBHOOK_URL=lego-alert-webhook-uat:latest" in recorded
    env_part = re.search(r"--set-env-vars=(\S+)", recorded).group(1)
    assert "ALERT_WEBHOOK_URL" not in env_part
    assert "Alert webhook  : Secret Manager (lego-alert-webhook-uat)" in result.stdout


def test_a_webhook_url_is_rejected_in_place_of_a_secret_name(sandbox):
    result = run(sandbox, observe_env(), ALERT_WEBHOOK_SECRET_OVERRIDE="https://hooks.example/abc?token=x")
    assert result.returncode == 1 and "must be a Secret Manager secret name" in result.stderr
    assert "token=x" not in result.stdout + log(sandbox)


def test_a_missing_webhook_is_called_out(sandbox):
    result = run(sandbox, observe_env())
    assert "Alert webhook  : NOT configured" in result.stdout
    assert "ALERT_WEBHOOK_URL" not in log(sandbox)


# ------------------------------------------- PROD observe in one run (cloudshell-prod-observe.sh)
PROD_DB = "https://lego-firebase-prod.asia-southeast1.firebasedatabase.app"
PROD_INSTANCES = json.dumps({"status": "success", "result": [
    {"name": "lego-firebase-prod", "location": "asia-southeast1", "project": "lego-firebase",
     "databaseUrl": PROD_DB, "type": "USER_DATABASE", "state": "ACTIVE"}]})


def run_prod_observe(sandbox, env):
    sandbox["log"].write_text("")
    process_env = {
        "PATH": f"{sandbox['stubs']}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(sandbox["repo"]), "STUB_LOG": str(sandbox["log"]),
        "PYTHONDONTWRITEBYTECODE": "1", "STUB_RTDB_INSTANCES": PROD_INSTANCES,
        "LEGO_DNA_BUNDLE_OVERRIDE": "strategy.long.json", **env}
    return subprocess.run(["bash", "deploy/cloudshell-prod-observe.sh"], cwd=sandbox["repo"],
                          env=process_env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=180)


def test_prod_observe_needs_the_principal_before_any_cloud_call(sandbox):
    result = run_prod_observe(sandbox, {})
    assert result.returncode == 1 and "LEGO_FIX_C_OVERRIDE is required" in result.stderr
    assert log(sandbox) == ""


def test_prod_observe_deploys_observe_only_whatever_the_shell_still_holds(sandbox):
    leftovers = release_env()                     # a live shell: active, caps, token, authorization
    leftovers.update(authorized(sandbox, leftovers), LEGO_PROD_LIVE_ACK="LIVE-PROD-UBER-left-over",
                     WEBULL_ENV_OVERRIDE="UAT", LEGO_STALE_ORDER_ACTION_OVERRIDE="cancel_expire")
    leftovers.pop("LEGO_MODE_OVERRIDE")           # the mode is an input of this script: trade is refused
    result = run_prod_observe(sandbox, {**leftovers, "LEGO_FIX_C_OVERRIDE": "10000"})
    assert result.returncode == 78, result.stderr  # the stub gcloud stops at `functions deploy`
    recorded = log(sandbox)
    # The PROD rules went to the PROD instance only, and anonymous reads were proven denied.
    assert ('{"database":[{"instance":"lego-firebase-prod","rules":"database.rules.prod.json"}]}'
            in recorded)
    for node in ("webull_lego_rows", "webull_lego_state", "webull_lego_order_audit",
                 "webull_lego_order_audit_archive", "webull_lego_warnings"):
        assert f"curl -sS --max-time 15 {PROD_DB}/{node}.json?shallow=true" in recorded
    assert "secrets versions add" not in recorded  # the stored token is still fresh
    deployed = re.search(r"functions deploy lego-tick-prod .*", recorded).group(0)
    for value in (f"WEBULL_ENV=PROD,FIREBASE_DB_URL={PROD_DB},LEGO_SYMBOL=UBER,LEGO_FIX_C=10000,",
                  "LEGO_MODE=observe,LEGO_ACTIVE=false,", "LEGO_RELEASE_AUTHORIZATION=,",
                  "LEGO_MAX_ORDER_QUANTITY=,LEGO_MAX_ORDER_NOTIONAL_USD=,LEGO_MAX_SESSION_ORDERS=,"
                  "LEGO_TRADING_WINDOW_END=,", "LEGO_STALE_ORDER_ACTION=hold,",
                  "LEGO_SESSION_KEY_MODE=market_day",
                  "WEBULL_TOKEN_SECRET=projects/lego-firebase/secrets/webull-token-prod"):
        assert value in deployed, value
    assert "LEGO_PROD_LIVE_ACK" not in recorded


def test_a_token_webull_still_accepts_needs_no_new_2fa(sandbox):
    result = run_prod_observe(sandbox, {"LEGO_FIX_C_OVERRIDE": "10000", "STUB_TOKEN_STATUS": "fresh"})
    assert result.returncode == 78, result.stderr  # the stub gcloud stops at `functions deploy`
    recorded = log(sandbox)
    # The app credentials reach Check Token through the environment, never argv.
    assert "python3 ops.py token-status WEBULL_ENV=PROD app_key=set" in recorded
    assert "Webull Check Token: status=NORMAL, expires=2026-12-01T00:00:00Z" in result.stdout
    assert "Open the Webull app now" not in result.stdout
    assert "secrets versions add" not in recorded
    assert "stub-token" not in result.stdout + result.stderr + recorded


def test_a_token_webull_rejects_goes_to_2fa_whatever_its_stored_expiry(sandbox):
    # The expiry stored with the stub token is ten days ahead; Check Token says INVALID.
    result = run_prod_observe(sandbox, {"LEGO_FIX_C_OVERRIDE": "10000", "STUB_TOKEN_STATUS": "rejected",
                                        "STUB_ISSUE_TOKEN_FAILS": "1"})
    assert result.returncode != 0
    assert "Webull Check Token: status=INVALID" in result.stdout
    assert "Open the Webull app now" in result.stdout
    recorded = log(sandbox)
    assert "python3 - (issue_token)" in recorded and "functions deploy" not in recorded


def test_when_webull_cannot_be_asked_the_stored_expiry_decides(sandbox):
    result = run_prod_observe(sandbox, {"LEGO_FIX_C_OVERRIDE": "10000"})  # Check Token unreachable
    assert result.returncode == 78, result.stderr
    assert "could not be asked; judging by the expiry stored with the token" in result.stdout
    assert "OK token: NORMAL, more than 3 days left" in result.stdout
    assert "Open the Webull app now" not in result.stdout


def test_prod_observe_stops_when_the_database_answers_anonymous_reads(sandbox):
    result = run_prod_observe(sandbox, {"LEGO_FIX_C_OVERRIDE": "10000",
                                        "STUB_RTDB_BODY": '{"UBER_x": true}'})
    assert result.returncode == 1 and "readable without signing in" in result.stderr
    assert "functions deploy" not in log(sandbox)


def test_the_prod_wrapper_never_deploys_trade(sandbox):
    result = run_prod_observe(sandbox, {"LEGO_FIX_C_OVERRIDE": "10000", "LEGO_MODE_OVERRIDE": "trade"})
    assert result.returncode == 1 and "must be observe, preview or paper" in result.stderr
    assert log(sandbox) == ""


# --------------------------------------- PROD preview: every step of the order path but Place
PREVIEW_INPUTS = {"LEGO_FIX_C_OVERRIDE": "10000", "LEGO_MODE_OVERRIDE": "preview",
                  "LEGO_REFERENCE_PRICE": "67.4", "LEGO_FUNDING_MODE_OVERRIDE": "initial-funding",
                  "LEGO_PREVIEW_CONFIRMED": "yes"}


def test_prod_preview_needs_webulls_word_on_preview_before_any_call(sandbox):
    inputs = {k: v for k, v in PREVIEW_INPUTS.items() if k != "LEGO_PREVIEW_CONFIRMED"}
    result = run_prod_observe(sandbox, inputs)
    assert result.returncode == 1 and "confirm with Webull" in result.stderr
    assert log(sandbox) == ""


def test_prod_preview_plans_its_own_release_and_deploys_without_an_ack(sandbox):
    result = run_prod_observe(sandbox, PREVIEW_INPUTS)
    assert result.returncode == 78, result.stderr  # the stub gcloud stops at `functions deploy`
    recorded = log(sandbox)
    deployed = re.search(r"functions deploy lego-tick-prod .*", recorded).group(0)
    assert "LEGO_MODE=preview,LEGO_ACTIVE=true," in deployed
    assert "LEGO_MAX_ORDER_NOTIONAL_USD=12500," in deployed        # funding caps: a flat account
    assert re.search(r"LEGO_RELEASE_AUTHORIZATION=[0-9a-f]{64},", deployed)
    assert re.search(r"LEGO_TRADING_WINDOW_END=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ,", deployed)
    assert "LEGO_PROD_LIVE_ACK" not in recorded and "REAL-MONEY RELEASE" not in result.stdout
    assert "Order submit   : preview only (true); Place is never called" in result.stdout


def test_production_preview_without_its_release_is_refused_before_deploy(sandbox):
    result = run(sandbox, release_env(LEGO_MODE_OVERRIDE="preview"))
    assert result.returncode == 1 and "EXPECTED_CANDIDATE_HASH" in result.stderr
    assert "FIREBASE_DEPLOY_REACHED" not in log(sandbox)


def test_production_preview_deploys_with_its_release_and_without_an_ack(sandbox):
    result = run(sandbox, authorized(sandbox, release_env(LEGO_MODE_OVERRIDE="preview")))
    assert result.returncode == 78, result.stderr
    deployed = re.search(r"functions deploy lego-tick-prod .*", log(sandbox)).group(0)
    assert "LEGO_MODE=preview,LEGO_ACTIVE=true," in deployed
    assert "LEGO_PROD_LIVE_ACK" not in log(sandbox) and "REAL-MONEY RELEASE" not in result.stdout


def test_an_inactive_production_preview_is_a_half_state(sandbox):
    result = run(sandbox, release_env(LEGO_MODE_OVERRIDE="preview", LEGO_ACTIVE_OVERRIDE="false"))
    assert result.returncode == 1 and "preview/active or paper/active (no Place)" in result.stderr
    assert log(sandbox) == ""


# ---------------------------------- PROD paper: preview + fills on paper holdings, own database
PAPER_DB = "https://lego-firebase-paper.asia-southeast1.firebasedatabase.app"
PAPER_INSTANCES = json.dumps({"status": "success", "result": [
    {"name": "lego-firebase-paper", "location": "asia-southeast1", "project": "lego-firebase",
     "databaseUrl": PAPER_DB, "type": "USER_DATABASE", "state": "ACTIVE"}]})
PAPER_INPUTS = {"LEGO_FIX_C_OVERRIDE": "10000", "LEGO_MODE_OVERRIDE": "paper",
                "LEGO_REFERENCE_PRICE": "67.4", "LEGO_PREVIEW_CONFIRMED": "yes",
                "STUB_RTDB_INSTANCES": PAPER_INSTANCES}


def test_prod_paper_keeps_its_holdings_in_a_database_of_its_own(sandbox):
    result = run_prod_observe(sandbox, PAPER_INPUTS)       # no funding answer: paper starts flat
    assert result.returncode == 78, result.stderr          # the stub gcloud stops at `functions deploy`
    recorded = log(sandbox)
    assert ('{"database":[{"instance":"lego-firebase-paper","rules":"database.rules.prod.json"}]}'
            in recorded)
    assert f"curl -sS --max-time 15 {PAPER_DB}/webull_lego_rows.json?shallow=true" in recorded
    assert "lego-firebase-prod" not in recorded
    deployed = re.search(r"functions deploy lego-tick-prod .*", recorded).group(0)
    assert f"FIREBASE_DB_URL={PAPER_DB}," in deployed
    assert "LEGO_MODE=paper,LEGO_ACTIVE=true," in deployed
    assert "LEGO_MAX_ORDER_NOTIONAL_USD=12500," in deployed        # funding caps: paper starts flat
    assert re.search(r"LEGO_RELEASE_AUTHORIZATION=[0-9a-f]{64},", deployed)
    assert "LEGO_PROD_LIVE_ACK" not in recorded and "REAL-MONEY RELEASE" not in result.stdout
    assert "Order submit   : paper (true)" in result.stdout


@pytest.mark.parametrize("mode,instance,message", [
    ("paper", "lego-firebase-prod", "a database named for paper"),
    ("preview", "lego-firebase-paper", "holds paper holdings"),
    ("observe", "lego-firebase-paper", "holds paper holdings"),
])
def test_the_prod_wrapper_never_mixes_paper_and_real_databases(sandbox, mode, instance, message):
    inputs = {**PAPER_INPUTS, "LEGO_MODE_OVERRIDE": mode, "LEGO_PROD_DB_INSTANCE": instance,
              "LEGO_FUNDING_MODE_OVERRIDE": "initial-funding"}
    result = run_prod_observe(sandbox, inputs)
    assert result.returncode == 1 and message in result.stderr
    assert log(sandbox) == ""


def test_production_paper_deploys_with_its_release_and_without_an_ack(sandbox):
    result = run(sandbox, authorized(sandbox, release_env(LEGO_MODE_OVERRIDE="paper")),
                 DATABASE_URL_OVERRIDE=PAPER_DB)
    assert result.returncode == 78, result.stderr
    deployed = re.search(r"functions deploy lego-tick-prod .*", log(sandbox)).group(0)
    assert "LEGO_MODE=paper,LEGO_ACTIVE=true," in deployed and f"FIREBASE_DB_URL={PAPER_DB}," in deployed
    assert "LEGO_PROD_LIVE_ACK" not in log(sandbox) and "REAL-MONEY RELEASE" not in result.stdout


def test_an_inactive_production_paper_is_a_half_state(sandbox):
    result = run(sandbox, release_env(LEGO_MODE_OVERRIDE="paper", LEGO_ACTIVE_OVERRIDE="false"))
    assert result.returncode == 1 and "paper/active (no Place)" in result.stderr
    assert log(sandbox) == ""


# ---------------------------------------------------------- captured deployments
def captured(env):
    from tools.verify_deployment import verify
    image, candidate = "registry/lego@sha256:" + "a" * 64, "b" * 64
    service = {"metadata": {"name": "lego-tick-prod"}, "spec": {"template": {
        "metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "1"}},
        "spec": {"containerConcurrency": 1, "timeoutSeconds": 45, "containers": [
            {"image": image, "env": [{"name": "LEGO_CANDIDATE_HASH", "value": candidate},
                                     *({"name": k, "value": v} for k, v in env.items())]}]}}},
        "status": {"latestReadyRevisionName": "v1", "traffic": [{"revisionName": "v1", "percent": 100}],
                   "url": "https://example.run.app"}}
    scheduler = {"schedule": "* * * * *", "retryConfig": {"retryCount": 0},
                 "httpTarget": {"uri": "https://example.run.app"}}
    return verify(service, scheduler, candidate=candidate, revision="v1", image=image)


LIVE = {"WEBULL_ENV": "PROD", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true",
        "LEGO_PROD_LIVE_ACK": "LIVE-PROD-UBER", "LEGO_RELEASE_AUTHORIZATION": "f" * 64,
        "LEGO_MAX_ORDER_QUANTITY": "20", "LEGO_MAX_ORDER_NOTIONAL_USD": "1000",
        "LEGO_MAX_SESSION_ORDERS": "10", "LEGO_TRADING_WINDOW_END": "2026-10-09T20:00:00Z"}


def test_a_captured_production_revision_may_be_observe_or_fully_live():
    assert captured({"WEBULL_ENV": "PROD", "LEGO_MODE": "observe", "LEGO_ACTIVE": "false"})["status"] == "PASS"
    assert captured(LIVE)["status"] == "PASS"


@pytest.mark.parametrize("missing", ["LEGO_PROD_LIVE_ACK", "LEGO_RELEASE_AUTHORIZATION",
                                     "LEGO_MAX_ORDER_QUANTITY", "LEGO_MAX_ORDER_NOTIONAL_USD",
                                     "LEGO_MAX_SESSION_ORDERS", "LEGO_TRADING_WINDOW_END"])
def test_a_live_production_revision_missing_any_requirement_fails(missing):
    report = captured({k: v for k, v in LIVE.items() if k != missing})
    assert report["status"] == "FAIL" and report["checks"]["production_mode"] is False


@pytest.mark.parametrize("mode,active", [("trade", "false"), ("observe", "true")])
def test_a_half_live_production_revision_fails(mode, active):
    assert captured({**LIVE, "LEGO_MODE": mode, "LEGO_ACTIVE": active})["status"] == "FAIL"


def test_a_captured_production_preview_needs_its_release_and_never_an_ack():
    preview = {**LIVE, "LEGO_MODE": "preview"}
    preview.pop("LEGO_PROD_LIVE_ACK")
    assert captured(preview)["status"] == "PASS"
    assert captured({**preview, "LEGO_PROD_LIVE_ACK": "LIVE-PROD-UBER"})["status"] == "FAIL"
    assert captured({**preview, "LEGO_RELEASE_AUTHORIZATION": ""})["status"] == "FAIL"
    assert captured({**preview, "LEGO_ACTIVE": "false"})["status"] == "FAIL"


def test_a_captured_paper_revision_writes_only_to_a_paper_database():
    paper = {**LIVE, "LEGO_MODE": "paper", "FIREBASE_DB_URL": PAPER_DB}
    paper.pop("LEGO_PROD_LIVE_ACK")
    assert captured(paper)["status"] == "PASS"
    assert captured({**paper, "LEGO_PROD_LIVE_ACK": "LIVE-PROD-UBER"})["status"] == "FAIL"
    report = captured({**paper, "FIREBASE_DB_URL": PROD_DB})
    assert report["status"] == "FAIL" and report["checks"]["database_matches_mode"] is False
    # A real chain never lands in the paper database either.
    assert captured({**paper, "LEGO_MODE": "preview"})["status"] == "FAIL"
    assert captured({**LIVE, "FIREBASE_DB_URL": PAPER_DB})["status"] == "FAIL"
