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
          "requirements.txt", "requirements-dev.txt", "deploy/cloudshell-all-in-one.sh"]

GCLOUD = r"""#!/usr/bin/env bash
echo "gcloud $*" >> "${STUB_LOG}"
case "$*" in
  "auth list"*) echo "tester@example.com" ;;
  "projects describe"*projectNumber*) echo "123456789" ;;
  "projects describe"*|"config set"*|"services enable"*) ;;
  "iam service-accounts describe"*|"secrets describe"*) ;;
  "secrets versions describe"*) echo "ENABLED" ;;
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
  deploy) echo "FIREBASE_DEPLOY_REACHED" >> "${STUB_LOG}" ;;
  *) echo "unexpected firebase call: $*" >&2; exit 99 ;;
esac
"""


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
    for name, body in (("gcloud", GCLOUD), ("firebase", FIREBASE)):
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
                 "LEGO_ALLOW_FRACTIONAL", "LEGO_SESSION_KEY_MODE", "LEGO_STALE_ORDER_ACTION",
                 "LEGO_STALE_ORDER_SECONDS", "LEGO_CANCEL_CONFIRM_GRACE_SECONDS"):
        assert exported(name) == policy[name], name
    for name in ("LEGO_DNA_BUNDLE", "LEGO_MAX_SESSION_ORDERS", "LEGO_MAX_ORDER_QUANTITY",
                 "LEGO_MAX_ORDER_NOTIONAL_USD"):
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
