"""Audit 2026-11-03: the PROD real-money release for QBTS, made fail-closed before any order.

The owner's parameters are pinned here exactly:
WEBULL_ENV=PROD LEGO_MODE=trade LEGO_ACTIVE=true LEGO_SYMBOL=QBTS LEGO_FIX_C=1500
LEGO_ALLOW_FRACTIONAL=true LEGO_DIFF=50 LEGO_LEAPS=100.

* T1  release-plan judges LEAPS against FIX_C (`leaps_value`): with 100 share-equivalents a
      BUY needs a price below (FIX_C - DIFF) / LEAPS = 14.50 and a SELL one below
      FIX_C / LEAPS = 15.00, so a release planned at or above 15 can never send an order.
* T2  the exact parameter set through config, plan, runtime gate and verify_deployment.
* T3  provenance: the candidate hash and the uploaded source both come from git HEAD, so a
      file git ignores can no longer make the deployed hash differ from the reviewed commit
      (CANDIDATE_MISMATCH of revisions 00003-dob and 00005-tul).
* T6  deploy/cloudshell-prod-live.sh, the one-command real-money release, in a sandbox with
      stub gcloud/firebase/curl and a stub account preflight: nothing reaches the cloud
      unless every local gate passes, and nothing deploys without the acknowledgement.
* T8  numbers of the PROD paper export audited here (rows of 2026-11-02) recomputed.

Nothing here touches a network, a broker or Secret Manager.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import release_horizon as rh
from config import load_runtime_config, release_binding_for
from test_deploy_scripts import (ACCOUNT, COPIED, CURL, FIREBASE, ORIGIN, PROD_DB,
                                 PROD_INSTANCES)
from tools.make_dna_bundle import build_bundle

ROOT = Path(__file__).parent
STATIC_WINDOW = "2026-11-10T21:00:00Z"      # static checks never read the clock
PRICE_NOW = "10.82"                         # QBTS, late October 2026 (web quote, unverified)


def plan_inputs(price=None, *, leaps="100", trading=True, environment="PROD", **overrides):
    values = dict(
        environment=environment, mode="trade" if trading else "observe", active=trading,
        symbol="QBTS", principal=Decimal("1500"), diff=Decimal("50"),
        limits=("20", "150", "10", STATIC_WINDOW), allow_fractional=True,
        dna_code="bypass:6500", dna_interval=900, dna_origin="2026-09-08T13:30:00Z",
        reference_price=None if price is None else Decimal(price),
        leaps_shares=Decimal(leaps))
    values.update(overrides)
    return rh.ReleaseInputs(**values)


def leaps_finding(inp):
    found = [f for f in rh.findings(inp) if f["id"] == "leaps_value"]
    assert len(found) <= 1
    return found[0] if found else None


# ------------------------------------------------------------------ T1 leaps_value
def test_the_price_band_of_100_leaps_against_1500():
    buy_below, sell_below = rh.leaps_price_band(Decimal("1500"), Decimal("50"), Decimal("100"))
    assert (buy_below, sell_below) == (Decimal("14.5"), Decimal("15"))


@pytest.mark.parametrize("price,severity", [
    ("10.82", "INFO"), ("14.4999", "INFO"),
    ("14.50", "WARN"), ("14.9999", "WARN"),
    ("15.00", "BLOCK"), ("15.20", "BLOCK"), ("40", "BLOCK"),
])
def test_leaps_value_judges_the_reference_price(price, severity):
    finding = leaps_finding(plan_inputs(price))
    assert finding is not None and finding["severity"] == severity, finding
    assert finding["static"] is True


def test_a_release_that_cannot_trade_is_only_a_warning_when_it_cannot_trade_anyway():
    assert leaps_finding(plan_inputs("15.20", trading=False))["severity"] == "WARN"


def test_the_info_names_the_real_share_target_and_the_flat_account_first_buy():
    message = leaps_finding(plan_inputs(PRICE_NOW))["message"]
    assert "38.63" in message            # 1500 / 10.82 - 100 real shares
    assert "418" in message              # 1500 - 100 x 10.82 USD: a flat account's first BUY
    assert "14.5" in message and "15" in message


def test_without_a_reference_price_the_band_is_a_warning_never_a_block():
    finding = leaps_finding(plan_inputs(None))
    assert finding["severity"] == "WARN"
    assert "14.5" in finding["message"] and "--reference-price" in finding["message"]


def test_without_leaps_every_finding_is_what_it_was():
    for price in (None, PRICE_NOW, "15.20"):
        plain = plan_inputs(price, leaps="0")
        assert leaps_finding(plain) is None
        legacy = {k: v for k, v in plain.__dict__.items() if k != "leaps_shares"}
        assert rh.findings(plain) == rh.findings(rh.ReleaseInputs(**legacy))


def test_a_blocked_leaps_release_gets_no_acknowledgement():
    assert rh.prod_live_acks(plan_inputs("15.20"), "f" * 64) == {
        "prefunded": None, "initial-funding": None}
    acks = rh.prod_live_acks(plan_inputs(PRICE_NOW), "f" * 64)
    assert acks["prefunded"] and acks["prefunded"].startswith("LIVE-PROD-QBTS-q20-n150-o10-")


def test_inputs_from_runtime_carry_the_leaps(tmp_path):
    runtime = load_runtime_config(exact_env(tmp_path))
    assert rh.inputs_from_runtime(runtime).leaps_shares == Decimal("100")


# ------------------------------------------------------------------ T2 the exact parameters
def exact_bundle(tmp_path):
    path = tmp_path / "strategy.uat-continuous.json"
    shutil.copy(ROOT / "strategy.uat-continuous.json", path)
    return str(path)


def exact_env(tmp_path, **overrides):
    window = rh.format_window_end(rh.window_end_after_sessions(datetime.now(timezone.utc), 5))
    values = {
        "WEBULL_ENV": "PROD", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true", "LEGO_SYMBOL": "QBTS",
        "LEGO_FIX_C": "1500", "LEGO_ALLOW_FRACTIONAL": "true", "LEGO_DIFF": "50", "LEGO_LEAPS": "100",
        "WEBULL_ACCOUNT_ID": ACCOUNT, "LEGO_DNA_BUNDLE": exact_bundle(tmp_path),
        "LEGO_CANDIDATE_HASH": "c" * 64, "LEGO_MAX_ORDER_QUANTITY": "20",
        "LEGO_MAX_ORDER_NOTIONAL_USD": "150", "LEGO_MAX_SESSION_ORDERS": "10",
        "LEGO_TRADING_WINDOW_END": window, "LEGO_SESSION_KEY_MODE": "market_day",
        "LEGO_STALE_ORDER_ACTION": "hold", "LEGO_STALE_ORDER_SECONDS": "300",
        "LEGO_CANCEL_CONFIRM_GRACE_SECONDS": "120", "LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER": "1",
        "FIREBASE_DB_URL": PROD_DB}
    values.update(overrides)
    return values


def authorized(values):
    return {**values, "LEGO_RELEASE_AUTHORIZATION": release_binding_for(values)}


def acked(values, funding="prefunded"):
    runtime = load_runtime_config(values)
    ack = rh.prod_live_acks(rh.inputs_from_runtime(runtime),
                            runtime.deployment.expected_release_binding)[funding]
    assert ack, "the exact release must be acknowledgeable"
    return {**values, "LEGO_PROD_LIVE_ACK": ack}


def test_the_exact_parameters_parse_into_one_runtime(tmp_path):
    runtime = load_runtime_config(exact_env(tmp_path))
    operator, deployment = runtime.operator, runtime.deployment
    assert (operator.symbol, operator.principal_usd, operator.diff_usd, operator.leaps_shares) == (
        "QBTS", 1500.0, 50.0, 100.0)
    assert (operator.mode, operator.active, operator.preview_only, operator.paper) == (
        "trade", True, False, False)
    assert (deployment.environment, deployment.allow_fractional) == ("PROD", True)
    assert operator.canonical()["leaps_shares"] == "100"


def test_leaps_100_is_its_own_release_binding(tmp_path):
    with_leaps = release_binding_for(exact_env(tmp_path))
    without = release_binding_for(exact_env(tmp_path, LEGO_LEAPS="0"))
    assert with_leaps != without                 # a release planned without LEAPS cannot carry them


def test_real_money_needs_the_acknowledgement_of_exactly_this_release(tmp_path):
    values = authorized(exact_env(tmp_path))
    runtime = load_runtime_config(values)
    assert runtime.deployment.release_is_authorized and runtime.prod_live_gate_open is False
    assert runtime.allows_new_broker_mutation is False
    opened = load_runtime_config(acked(values))
    assert opened.prod_live_gate_open is True and opened.allows_new_broker_mutation is True
    # The acknowledgement of the LEAPS-less release does not open this one.
    plain = acked(authorized(exact_env(tmp_path, LEGO_LEAPS="0")))
    assert load_runtime_config({**values, "LEGO_PROD_LIVE_ACK": plain["LEGO_PROD_LIVE_ACK"]}
                               ).prod_live_gate_open is False


def test_release_plan_of_the_exact_parameters_at_todays_price(tmp_path, monkeypatch, capsys):
    import ops
    values = exact_env(tmp_path)
    for key in [k for k in list(__import__("os").environ) if k.startswith(("LEGO_", "WEBULL_"))]:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        if not key.startswith("LEGO_MAX_") and key != "LEGO_TRADING_WINDOW_END":
            monkeypatch.setenv(key, value)
    args = ops.parser().parse_args([
        "release-plan", "--window-sessions", "5", "--reference-price", PRICE_NOW,
        "--recommended-limits", "--candidate-hash", "c" * 64, "--fee-pct", "0.35", "--enforce"])
    result = ops.release_plan_command(args)
    assessment = result["assessment"]
    assert assessment["ok"] and not result["blocked"], assessment["blocking"]
    assert (result["release"]["LEGO_MAX_ORDER_QUANTITY"], result["release"]["LEGO_MAX_ORDER_NOTIONAL_USD"],
            result["release"]["LEGO_MAX_SESSION_ORDERS"]) == ("20", "150", "10")
    assert "fractional_policy" in assessment["warnings"]
    finding = next(f for f in assessment["findings"] if f["id"] == "leaps_value")
    assert finding["severity"] == "INFO" and "38.63" in finding["message"]
    ack = result["prod_live_acks"]["prefunded"]
    assert re.fullmatch(r"LIVE-PROD-QBTS-q20-n150-o10-\d{8}T\d{4}Z-prefunded-[0-9a-f]{16}", ack)
    assert result["deploy_env"]["LEGO_LEAPS_OVERRIDE"] == "100"
    assert result["deploy_env"]["LEGO_ALLOW_FRACTIONAL_OVERRIDE"] == "true"
    blocked = ops.release_plan_command(ops.parser().parse_args([
        "release-plan", "--window-sessions", "5", "--reference-price", "15.20",
        "--recommended-limits", "--candidate-hash", "c" * 64, "--enforce"]))
    assert blocked["blocked"] and "leaps_value" in blocked["assessment"]["blocking"]
    assert "prod_live_acks" not in blocked or not any(blocked["prod_live_acks"].values())


def test_the_deployed_revision_of_the_exact_parameters_verifies():
    from test_deploy_scripts import captured
    live = {"WEBULL_ENV": "PROD", "LEGO_MODE": "trade", "LEGO_ACTIVE": "true", "LEGO_SYMBOL": "QBTS",
            "LEGO_FIX_C": "1500", "LEGO_ALLOW_FRACTIONAL": "true", "LEGO_DIFF": "50", "LEGO_LEAPS": "100",
            "LEGO_PROD_LIVE_ACK": "LIVE-PROD-QBTS-q20-n150-o10-20261110T2100Z-prefunded-0123456789abcdef",
            "LEGO_RELEASE_AUTHORIZATION": "f" * 64, "LEGO_MAX_ORDER_QUANTITY": "20",
            "LEGO_MAX_ORDER_NOTIONAL_USD": "150", "LEGO_MAX_SESSION_ORDERS": "10",
            "LEGO_TRADING_WINDOW_END": "2026-11-10T21:00:00Z", "FIREBASE_DB_URL": PROD_DB}
    report = captured(live)
    assert report["status"] == "PASS", report["checks"]
    assert report["checks"]["production_mode"] and report["checks"]["database_matches_mode"]
    paper_db = captured({**live, "FIREBASE_DB_URL": PROD_DB.replace("prod", "paper")})
    assert paper_db["status"] == "FAIL"


# ------------------------------------------------------------------ T3 provenance (unit)
def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def small_repo(tmp_path):
    repo = tmp_path / "parent" / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "candidate_manifest.py", repo / "tools" / "candidate_manifest.py")
    shutil.copy(ROOT / ".gitignore", repo / ".gitignore")
    for name in ("requirements.txt", "requirements-dev.txt"):
        shutil.copy(ROOT / name, repo / name)
    (repo / "main.py").write_text("print('reviewed')\n", encoding="utf-8")
    (repo / "policy.json").write_text('{"a": 1}\n', encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "reviewed")
    return repo


def manifest(repo, *args):
    out = subprocess.run([sys.executable, "tools/candidate_manifest.py", *args], cwd=repo,
                         check=True, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}).stdout
    return json.loads(out)


def test_from_head_is_the_walk_of_a_clean_checkout(small_repo):
    walk, head = manifest(small_repo), manifest(small_repo, "--from-head")
    assert head["candidate_hash"] == walk["candidate_hash"]
    assert head["scope"] == walk["scope"] == "backend-only"
    assert head["files"] == walk["files"]
    assert head["source"] == "git HEAD " + _git(small_repo, "rev-parse", "HEAD").strip()


def test_ignored_files_and_a_sibling_reader_change_the_walk_but_not_head(small_repo):
    clean = manifest(small_repo, "--from-head")["candidate_hash"]
    (small_repo / "conf").mkdir()
    (small_repo / "conf" / "token.txt").write_text("secret-ish\n", encoding="utf-8")   # git-ignored
    (small_repo / "venv" / "lib").mkdir(parents=True)
    (small_repo / "venv" / "lib" / "x.py").write_text("x = 1\n", encoding="utf-8")       # git-ignored
    reader = small_repo.parent / "lego-firebase-streamlit"
    reader.mkdir()
    (reader / "app.py").write_text("y = 2\n", encoding="utf-8")
    assert _git(small_repo, "status", "--porcelain", "--untracked-files=all") == ""   # invisible to git
    walk = manifest(small_repo)
    assert walk["candidate_hash"] != clean and walk["scope"] == "backend-and-reader"
    assert "backend/conf/token.txt" in walk["files"]
    head = manifest(small_repo, "--from-head")
    assert head["candidate_hash"] == clean and head["scope"] == "backend-only"


def test_export_to_writes_exactly_the_hashed_tree(small_repo, tmp_path):
    (small_repo / "conf").mkdir()
    (small_repo / "conf" / "token.txt").write_text("secret-ish\n", encoding="utf-8")
    target = tmp_path / "upload" / "src"
    head = manifest(small_repo, "--from-head", "--export-to", str(target))
    exported = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert "conf/token.txt" not in exported and ".git" not in {p.split("/")[0] for p in exported}
    assert sorted(name.removeprefix("backend/") for name in head["files"]) == sorted(
        name for name in exported if name.endswith((".py", ".json", ".txt")) or name == ".gitignore")
    with pytest.raises(subprocess.CalledProcessError):           # never into a directory in use
        manifest(small_repo, "--from-head", "--export-to", str(target))


def test_build_manifest_still_reads_root_at_call_time(tmp_path):
    """tools.trace_audit swaps ROOT/STREAMLIT before calling build_manifest()."""
    from tools import candidate_manifest as cm
    other = tmp_path / "other"
    other.mkdir()
    for name in ("requirements.txt", "requirements-dev.txt"):
        shutil.copy(ROOT / name, other / name)
    (other / "only_here.py").write_text("x = 1\n", encoding="utf-8")
    saved = (cm.ROOT, cm.STREAMLIT)
    try:
        cm.ROOT, cm.STREAMLIT = other, tmp_path / "no-reader-here"
        manifest = cm.build_manifest()
    finally:
        cm.ROOT, cm.STREAMLIT = saved
    assert sorted(manifest["files"]) == ["backend/only_here.py", "backend/requirements-dev.txt",
                                         "backend/requirements.txt"]
    assert manifest["scope"] == "backend-only"


# ------------------------------------------------------------------ sandbox (T3 deploy, T6 wrapper)
GCLOUD_LISTING = r"""#!/usr/bin/env bash
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
  "functions deploy"*)
    for arg in "$@"; do case "${arg}" in --source=*) src="${arg#--source=}" ;; esac; done
    if [[ -n "${src:-}" && -d "${src}" ]]; then
      (cd "${src}" && find . -type f | LC_ALL=C sort | sed 's/^/SOURCE_FILE /') >> "${STUB_LOG}"
    fi
    echo "GCLOUD_FUNCTIONS_DEPLOY_REACHED" >> "${STUB_LOG}"; exit 78 ;;
  *) echo "unexpected gcloud call: $*" >&2; exit 99 ;;
esac
"""
# test_deploy_scripts.PYTHON3 plus the account preflight, which reads a live broker: here it
# answers what STUB_PREFLIGHT_JSON says and records how it was called (secrets: set or not).
PYTHON3_PREFLIGHT = r"""#!/usr/bin/env bash
if [[ "${1:-}" == ops.py && "${2:-}" == token-status ]]; then
  cat > /dev/null
  echo "python3 ops.py token-status WEBULL_ENV=${WEBULL_ENV:-} app_key=${WEBULL_APP_KEY:+set}" >> "${STUB_LOG}"
  case "${STUB_TOKEN_STATUS:-unreachable}" in
    fresh) echo '{"status": "NORMAL", "live_expires_at": "2026-12-01T00:00:00Z", "days_left": 30.0, "fresh": true, "exit_code": 0}'; exit 0 ;;
    rejected) echo '{"status": "INVALID", "live_expires_at": null, "days_left": null, "fresh": false, "exit_code": 1}'; exit 1 ;;
    *) echo '{"error": "Check Token unavailable: ConnectionError", "exit_code": 2}'; exit 2 ;;
  esac
fi
if [[ "${1:-}" == tools/prod_live_preflight.py ]]; then
  echo "python3 $* WEBULL_ENV=${WEBULL_ENV:-} app_key=${WEBULL_APP_KEY:+set} app_secret=${WEBULL_APP_SECRET:+set} account=${WEBULL_ACCOUNT_ID:+set} token_secret=${WEBULL_TOKEN_SECRET:-} token_dir=${WEBULL_TOKEN_DIR:+set} debug=${WEBULL_API_DEBUG:-}" >> "${STUB_LOG}"
  printf '%s' "${STUB_PREFLIGHT_JSON:-}"
  exit "${STUB_PREFLIGHT_STATUS:-0}"
fi
if [[ "${1:-}" == - && -n "${STUB_ISSUE_TOKEN_FAILS:-}" ]]; then
  cat > /dev/null
  echo "python3 - (issue_token)" >> "${STUB_LOG}"
  exit 1
fi
exec "@REAL_PYTHON@" "$@"
""".replace("@REAL_PYTHON@", sys.executable)

LIVE_FILES = ["deploy/cloudshell-prod-live.sh", "tools/prod_live_preflight.py",
              "strategy.uat-continuous.json"]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    repo = tmp_path_factory.mktemp("live-parent") / "repo"
    repo.mkdir()
    stubs = tmp_path_factory.mktemp("live-stubs")
    for name in COPIED + LIVE_FILES:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, repo / name)
    with (repo / "market_clock.py").open("a") as handle:
        handle.write('\n\ndef is_regular_session(at=None):  # test override\n'
                     '    return os.environ.get("FAKE_SESSION") == "1"\n')
    (repo / "strategy.long.json").write_text(
        json.dumps(build_bundle("bypass:20000", ORIGIN, 900), indent=2) + "\n")
    for name, body in (("gcloud", GCLOUD_LISTING), ("firebase", FIREBASE), ("curl", CURL),
                       ("python3", PYTHON3_PREFLIGHT)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sandbox")
    walk = subprocess.run([sys.executable, "tools/candidate_manifest.py"], cwd=repo,
                          check=True, capture_output=True, text=True)
    return {"repo": repo, "stubs": stubs, "log": stubs / "stub.log",
            "candidate": json.loads(walk.stdout)["candidate_hash"]}


def log(sandbox):
    return sandbox["log"].read_text()


def process_env(sandbox, env):
    return {"PATH": f"{sandbox['stubs']}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(sandbox["repo"]), "STUB_LOG": str(sandbox["log"]),
            "PYTHONDONTWRITEBYTECODE": "1", **env}


def run_script(sandbox, script, env):
    sandbox["log"].write_text("")
    return subprocess.run(["bash", script], cwd=sandbox["repo"], env=process_env(sandbox, env),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=240)


def observe_env():
    return {"WEBULL_ENV_OVERRIDE": "PROD", "LEGO_MODE_OVERRIDE": "observe", "LEGO_ACTIVE_OVERRIDE": "false",
            "LEGO_SYMBOL_OVERRIDE": "QBTS", "LEGO_FIX_C_OVERRIDE": "1500", "LEGO_DIFF_OVERRIDE": "50",
            "LEGO_LEAPS_OVERRIDE": "100", "LEGO_ALLOW_FRACTIONAL_OVERRIDE": "true",
            "LEGO_DNA_BUNDLE_OVERRIDE": "strategy.long.json", "LEGO_SESSION_KEY_MODE_OVERRIDE": "market_day",
            "DATABASE_URL_OVERRIDE": PROD_DB,
            "WEBULL_TOKEN_SECRET_OVERRIDE": "projects/lego-firebase/secrets/webull-token-prod"}


@pytest.fixture
def polluted(sandbox):
    """Files git ignores in the checkout the deploy runs from (as 00003/00005 may have had)."""
    repo = sandbox["repo"]
    made = [repo / "conf" / "token.txt", repo / "venv" / "lib" / "evil.py", repo / "x-credentials.json"]
    for path in made:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not reviewed\n", encoding="utf-8")
    reader = repo.parent / "lego-firebase-streamlit"
    reader.mkdir(exist_ok=True)
    (reader / "app.py").write_text("y = 2\n", encoding="utf-8")
    yield made
    shutil.rmtree(repo / "conf"), shutil.rmtree(repo / "venv"), shutil.rmtree(reader)
    (repo / "x-credentials.json").unlink()


def deployed_line(sandbox):
    return re.search(r"gcloud functions deploy lego-tick-prod .*", log(sandbox)).group(0)


def source_files(sandbox):
    return [line.removeprefix("SOURCE_FILE ./") for line in log(sandbox).splitlines()
            if line.startswith("SOURCE_FILE ")]


def test_the_deploy_hashes_and_uploads_git_head_whatever_the_checkout_holds(sandbox, polluted):
    result = run_script(sandbox, "deploy/cloudshell-all-in-one.sh", observe_env())
    assert result.returncode == 78, result.stdout[-2000:] + result.stderr   # stub stops at deploy
    assert f"LEGO_CANDIDATE_HASH={sandbox['candidate']}," in deployed_line(sandbox)
    uploaded = source_files(sandbox)
    assert "config.py" in uploaded and "deploy/cloudshell-all-in-one.sh" in uploaded
    for path in ("conf/token.txt", "venv/lib/evil.py", "x-credentials.json"):
        assert path not in uploaded, path
    assert not any(name.startswith(".git/") for name in uploaded)
    assert "--source=." not in log(sandbox)


def test_a_reviewed_candidate_deploys_from_a_polluted_checkout(sandbox, polluted):
    """Before: the walk hashed the ignored files and refused the reviewed candidate."""
    env = {**observe_env(), "EXPECTED_CANDIDATE_HASH": sandbox["candidate"]}
    result = run_script(sandbox, "deploy/cloudshell-all-in-one.sh", env)
    assert result.returncode == 78, result.stdout[-2000:] + result.stderr
    assert "ไม่ตรงกับ release evidence" not in result.stderr


# ------------------------------------------------------------------ T6 the live wrapper
EXACT = {"LEGO_SYMBOL_OVERRIDE": "QBTS", "LEGO_FIX_C_OVERRIDE": "1500", "LEGO_DIFF_OVERRIDE": "50",
         "LEGO_LEAPS_OVERRIDE": "100", "LEGO_ALLOW_FRACTIONAL_OVERRIDE": "true",
         "LEGO_ALERTS_CONFIRMED": "yes", "ALERT_WEBHOOK_SECRET_OVERRIDE": "lego-alert-webhook-prod",
         "LEGO_FEE_PCT_OVERRIDE": "0.35"}
PREFLIGHT_OK = {
    "schema": "lego_prod_live_preflight_v1", "ok": True, "environment": "Production",
    "broker_mutations": False, "blocking": [], "warnings": [],
    "findings": [{"id": "buying_power", "severity": "INFO", "message": "USD buying power ok"}],
    "derived": {"reference_price": PRICE_NOW, "real_shares": "38", "leaps_declared": "100",
                "leaps_held": "100", "real_target": "38.63", "suggested_real_shares": "38",
                "prefund_range": ["25", "52"], "required_cash_usd": "1515.00",
                "buying_power_usd": "1600.00", "first_order": None,
                "quote_time": "2026-11-02T21:00:00.000Z",
                "price_band": {"buy_below": "14.5", "sell_below": "15"}}}
ACK = re.compile(r"LEGO_PROD_LIVE_ACK=(LIVE-PROD-QBTS-q20-n150-o10-\d{8}T\d{4}Z-prefunded-[0-9a-f]{16})")


def run_live(sandbox, env=None, *, preflight=None, status=0):
    inputs = {**EXACT, "STUB_RTDB_INSTANCES": PROD_INSTANCES, "STUB_TOKEN_STATUS": "fresh",
              "STUB_PREFLIGHT_JSON": json.dumps(PREFLIGHT_OK if preflight is None else preflight),
              "STUB_PREFLIGHT_STATUS": str(status), **(env or {})}
    return run_script(sandbox, "deploy/cloudshell-prod-live.sh",
                      {k: v for k, v in inputs.items() if v is not None})


@pytest.mark.parametrize("missing", ["LEGO_SYMBOL_OVERRIDE", "LEGO_FIX_C_OVERRIDE", "LEGO_DIFF_OVERRIDE",
                                     "LEGO_LEAPS_OVERRIDE", "LEGO_ALLOW_FRACTIONAL_OVERRIDE"])
def test_every_money_value_is_the_owners_and_has_no_default(sandbox, missing):
    result = run_live(sandbox, {missing: None})
    assert result.returncode == 1 and "no default" in result.stderr
    assert log(sandbox) == ""


@pytest.mark.parametrize("env,message", [
    ({"LEGO_ALERTS_CONFIRMED": None}, "LEGO_ALERTS_CONFIRMED=yes"),
    ({"LEGO_ALERTS_CONFIRMED": "y"}, "LEGO_ALERTS_CONFIRMED=yes"),
    ({"LEGO_PROD_DB_INSTANCE": "lego-firebase-paper"}, "paper"),
    ({"LEGO_PROD_DB_INSTANCE": "lego-firebase-default-rtdb"}, "default instance"),
    ({"LEGO_FUNDING_MODE_OVERRIDE": "loose"}, "must be prefunded (the canary) or initial-funding"),
    ({"LEGO_ALLOW_FRACTIONAL_OVERRIDE": "yes"}, "LEGO_ALLOW_FRACTIONAL_OVERRIDE"),
    ({"LEGO_FIX_C_OVERRIDE": "1,500"}, "LEGO_FIX_C_OVERRIDE"),
    ({"LEGO_LEAPS_OVERRIDE": "-1"}, "LEGO_LEAPS_OVERRIDE"),
    ({"LEGO_SYMBOL_OVERRIDE": "qbts;rm"}, "LEGO_SYMBOL_OVERRIDE"),
    ({"ALERT_WEBHOOK_SECRET_OVERRIDE": "https://hooks.example/x?token=y"}, "secret name"),
    ({"WEBULL_API_DEBUG": "1"}, "WEBULL_API_DEBUG"),
    ({"FAKE_SESSION": "1"}, "regular session"),
])
def test_local_gates_refuse_before_any_cloud_call(sandbox, env, message):
    result = run_live(sandbox, env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert log(sandbox) == ""
    assert "token=y" not in result.stdout + result.stderr


def test_a_dirty_checkout_is_refused_before_any_cloud_call(sandbox):
    stray = sandbox["repo"] / "stray.txt"
    stray.write_text("uncommitted\n", encoding="utf-8")
    try:
        result = run_live(sandbox)
    finally:
        stray.unlink()
    assert result.returncode == 1 and "commit or stash" in result.stderr
    assert log(sandbox) == ""


def test_the_exact_release_stops_at_the_acknowledgement(sandbox):
    result = run_live(sandbox)
    assert result.returncode == 3, result.stdout[-3000:] + result.stderr
    assert ACK.search(result.stdout), result.stdout[-3000:]
    recorded = log(sandbox)
    assert "GCLOUD_FUNCTIONS_DEPLOY_REACHED" not in recorded and "functions deploy" not in recorded
    # The only rules deployed are the PROD rules, to the PROD instance (the deploy script's own
    # step 8, the default instance's public rules, comes after the acknowledgement only).
    deploys = [line for line in recorded.splitlines() if "FIREBASE_DEPLOY_REACHED" in line]
    assert len(deploys) == 1 and '"instance":"lego-firebase-prod"' in deploys[0]
    # The PROD rules went to the PROD instance and anonymous reads were proven denied.
    assert '{"database":[{"instance":"lego-firebase-prod","rules":"database.rules.prod.json"}]}' in recorded
    assert f"curl -sS --max-time 15 {PROD_DB}/webull_lego_state.json?shallow=true" in recorded
    # The account preflight read the broker with secrets in its environment, never argv.
    call = next(line for line in recorded.splitlines() if "tools/prod_live_preflight.py" in line)
    assert "--symbol QBTS --fix-c 1500 --diff 50 --leaps 100 --allow-fractional true" in call
    assert "WEBULL_ENV=PROD app_key=set app_secret=set account=set" in call
    assert "token_secret=projects/lego-firebase/secrets/webull-token-prod token_dir=set debug=" in call
    assert "test-account-id" not in result.stdout + result.stderr
    assert "stub-token" not in result.stdout + result.stderr + recorded
    # What the owner reads before acknowledging, and how to stop.
    for text in ("QBTS / 1500 / 50 / 100 / true", "10.82", "38.63", "1515.00",
                 "20 / 150 USD / 10 per session", "BUY below 14.5, SELL below 15",
                 "ops.py halt-orders", "--apply",
                 "LEGO_SYMBOL_OVERRIDE=QBTS LEGO_FIX_C_OVERRIDE=1500 LEGO_DIFF_OVERRIDE=50 "
                 "LEGO_LEAPS_OVERRIDE=100 bash deploy/cloudshell-prod-observe.sh"):
        assert text in result.stdout, text


def test_the_acknowledged_release_deploys_exactly_the_requested_parameters(sandbox):
    first = run_live(sandbox)
    ack = ACK.search(first.stdout).group(1)
    result = run_live(sandbox, {"LEGO_PROD_LIVE_ACK": ack})
    assert result.returncode == 78, result.stdout[-3000:] + result.stderr   # stub stops at deploy
    assert "PROD live acknowledgement verified" in result.stdout
    deployed = deployed_line(sandbox)
    for value in (f"WEBULL_ENV=PROD,FIREBASE_DB_URL={PROD_DB},LEGO_SYMBOL=QBTS,LEGO_FIX_C=1500,"
                  "LEGO_ALLOW_FRACTIONAL=true,LEGO_DIFF=50,LEGO_DNA_BUNDLE=strategy.uat-continuous.json,"
                  "LEGO_MODE=trade,LEGO_ACTIVE=true,",
                  "LEGO_MAX_ORDER_QUANTITY=20,LEGO_MAX_ORDER_NOTIONAL_USD=150,LEGO_MAX_SESSION_ORDERS=10,",
                  "LEGO_LEAPS=100,", "LEGO_STALE_ORDER_ACTION=hold,", "LEGO_SESSION_KEY_MODE=market_day",
                  f"LEGO_PROD_LIVE_ACK={ack}", f"LEGO_CANDIDATE_HASH={sandbox['candidate']},",
                  "WEBULL_TOKEN_SECRET=projects/lego-firebase/secrets/webull-token-prod",
                  "ALERT_WEBHOOK_URL=lego-alert-webhook-prod:latest"):
        assert value in deployed, value
    assert re.search(r"LEGO_RELEASE_AUTHORIZATION=[0-9a-f]{64},", deployed)
    assert "--source=." not in deployed and "config.py" in source_files(sandbox)


def test_a_preflight_block_stops_before_the_release_is_planned(sandbox):
    blocked = {**PREFLIGHT_OK, "ok": False, "blocking": ["buying_power"], "findings": [
        {"id": "buying_power", "severity": "BLOCK",
         "message": "USD buying power 900.00 is below the 1515.00 this release may need"}]}
    result = run_live(sandbox, preflight=blocked, status=1)
    assert result.returncode == 1
    assert "[BLOCK] buying_power" in result.stdout and "preflight" in result.stderr
    assert "functions deploy" not in log(sandbox) and "LEGO_PROD_LIVE_ACK=" not in result.stdout


def test_a_reference_price_above_fix_c_over_leaps_is_blocked_by_the_plan(sandbox):
    above = {**PREFLIGHT_OK, "derived": {**PREFLIGHT_OK["derived"], "reference_price": "15.20"}}
    result = run_live(sandbox, preflight=above)
    assert result.returncode == 1
    assert "[BLOCK] leaps_value" in result.stdout
    assert "functions deploy" not in log(sandbox) and "LEGO_PROD_LIVE_ACK=" not in result.stdout


def test_an_unreadable_preflight_answer_is_a_block(sandbox):
    result = run_live(sandbox, env={"STUB_PREFLIGHT_JSON": "not json"})
    assert result.returncode == 1 and "functions deploy" not in log(sandbox)


def test_a_rejected_token_goes_to_2fa_before_anything_is_planned(sandbox):
    result = run_live(sandbox, {"STUB_TOKEN_STATUS": "rejected", "STUB_ISSUE_TOKEN_FAILS": "1"})
    assert result.returncode != 0 and "Open the Webull app now" in result.stdout
    recorded = log(sandbox)
    assert "python3 - (issue_token)" in recorded and "prod_live_preflight" not in recorded


def test_the_live_wrapper_is_the_only_wrapper_that_trades():
    observe = (ROOT / "deploy" / "cloudshell-prod-observe.sh").read_text(encoding="utf-8")
    live = (ROOT / "deploy" / "cloudshell-prod-live.sh").read_text(encoding="utf-8")
    assert "must be observe, preview or paper here" in observe          # unchanged
    # The live wrapper plans a trade/active release and refuses a plan that is anything else.
    assert "WEBULL_ENV=PROD LEGO_MODE=trade LEGO_ACTIVE=true" in live
    assert '"${LEGO_MODE_OVERRIDE:-}" == trade' in live and '"${LEGO_ACTIVE_OVERRIDE:-}" == true' in live
    assert "unset LEGO_PROD_LIVE_ACK" not in live                       # the ack is the owner's input
    # The loose funding caps are planned only in the funding mode (2026-11-03 funding addendum).
    assert '[[ "${FUNDING}" != initial-funding ]] || PLAN_ARGS+=(--initial-funding)' in live
    assert live.count("--initial-funding") == 1


# ------------------------------------------------------------------ T8 numbers of this export
# lego-firebase-paper export, chain UBER (FIX_C 3000, DIFF 5, LEGO_LEAPS 1), the 26 rows of the
# 2026-11-02 session: slot, decision price Pn, real (paper) shares, status, quantity, column 7.
SESSION_20261102 = [
    (0, 66.23, 46.16, "READY_SELL", 1.86, 47.16), (1, 66.8006, 44.3, "READY_SELL", 0.39, 45.3),
    (2, 66.91, 43.91, "PASS_THRESHOLD", 0, 44.91), (3, 67.24, 43.91, "READY_SELL", 0.29, 44.91),
    (4, 67.2667, 43.62, "PASS_THRESHOLD", 0, 44.62), (5, 67.33, 43.62, "PASS_THRESHOLD", 0, 44.62),
    (6, 67.18, 43.62, "PASS_THRESHOLD", 0, 44.62), (7, 67.21, 43.62, "PASS_THRESHOLD", 0, 44.62),
    (8, 67.26, 43.62, "PASS_THRESHOLD", 0, 44.62), (9, 67.3203, 43.62, "PASS_THRESHOLD", 0, 44.62),
    (10, 67.39, 43.62, "READY_SELL", 0.1, 44.62), (11, 67.27, 43.52, "READY_BUY", 0.07, 44.52),
    (12, 67.46, 43.59, "READY_SELL", 0.11, 44.59), (13, 67.225, 43.48, "READY_BUY", 0.14, 44.48),
    (14, 67.205, 43.62, "PASS_THRESHOLD", 0, 44.62), (15, 67.3414, 43.62, "PASS_THRESHOLD", 0, 44.62),
    (16, 67.25, 43.62, "PASS_THRESHOLD", 0, 44.62), (17, 67.085, 43.62, "READY_BUY", 0.09, 44.62),
    (18, 67.05, 43.71, "PASS_THRESHOLD", 0, 44.71), (19, 66.865, 43.71, "READY_BUY", 0.15, 44.71),
    (20, 66.83, 43.86, "PASS_THRESHOLD", 0, 44.86), (21, 66.58, 43.86, "READY_BUY", 0.19, 44.86),
    (22, 66.62, 44.05, "PASS_THRESHOLD", 0, 45.05), (23, 66.595, 44.05, "PASS_THRESHOLD", 0, 45.05),
    (24, 66.695, 44.05, "PASS_THRESHOLD", 0, 45.05), (25, 66.51, 44.05, "PASS_THRESHOLD", 0, 45.05),
]


def _paper_cfg(leaps):
    from lego_one_row import Config
    return Config("UBER", 3000.0, 5.0, "bypass:6500", "shannon_demon_lego_paper_v2",
                  decimal_precision=2, quantity_increment=0.01, leaps_shares=leaps)


@pytest.mark.parametrize("slot,price,real,status,quantity,column7", SESSION_20261102)
def test_every_decision_of_2026_11_02_recomputes_with_one_leaps_share(
        slot, price, real, status, quantity, column7):
    from lego_one_row import build_decision, effective_holdings
    decision = build_decision(_paper_cfg(1.0), price, real, 1)
    assert (decision.status, decision.quantity) == (status, quantity), slot
    assert effective_holdings(real, 1.0) == column7


def test_the_leaps_rollover_row_sold_one_share_more_than_without_it():
    """Row v27 (14:30Z): 46.16 + 1 LEAPS share at 66.23 = 3123.41 -> SELL 1.86 (0.86 without)."""
    from lego_one_row import build_decision
    with_leaps = build_decision(_paper_cfg(1.0), 66.23, 46.16, 1)
    assert with_leaps.value == pytest.approx(3123.4068) and with_leaps.gap == pytest.approx(123.4068)
    assert build_decision(_paper_cfg(0.0), 66.23, 46.16, 1).quantity == 0.86
    # The fill booked 1.86 at 66.11: real 46.16 -> 44.30, column 7 47.16 -> 45.30.
    assert round(46.16 - 1.86, 2) == 44.3
