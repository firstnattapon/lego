"""deploy/cloudshell-prod-live-all.sh: every PROD bot in one run (2026-11-07).

Most tests run the wrapper in a throwaway git checkout where deploy/cloudshell-prod-live.sh is
a stub that records what one bot received and can be told to fail: the table, the selection,
the shared local gates, the environment each bot gets and the run going on past a failed bot.
The last one runs it over the real deploy/cloudshell-prod-live.sh in the stubbed cloud of
test_audit_20261103.py, with the three bots already running, up to `gcloud functions deploy`.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_audit_20261103 import PREFLIGHT_OK, log, run_script, sandbox  # noqa: F401  (fixture)
from test_deploy_scripts import PROD_INSTANCES
from test_second_bot_20261103 import bot_env, services
from tools.bot_inventory import function_name

ROOT = Path(__file__).parent
SCRIPT = "deploy/cloudshell-prod-live-all.sh"
RECORDED = ["LEGO_SYMBOL_OVERRIDE", "LEGO_FIX_C_OVERRIDE", "LEGO_DIFF_OVERRIDE",
            "LEGO_LEAPS_OVERRIDE", "LEGO_ALLOW_FRACTIONAL_OVERRIDE", "LEGO_BOT_OVERRIDE",
            "LEGO_LEAPS_CONFIRMED", "LEGO_ALERTS_CONFIRMED", "LEGO_FEE_PCT_OVERRIDE",
            "ALERT_WEBHOOK_SECRET_OVERRIDE", "LEGO_FUNDING_MODE_OVERRIDE",
            "LEGO_RETIRE_SYMBOL_OVERRIDE", "LEGO_MAX_ORDER_QUANTITY_OVERRIDE",
            "LEGO_RELEASE_AUTHORIZATION_OVERRIDE", "EXPECTED_CANDIDATE_HASH", "LEGO_PROD_DB_INSTANCE"]
STUB = r"""#!/usr/bin/env bash
line="CALL"
for name in @RECORDED@; do
  if [[ -v "${name}" ]]; then line+=" ${name}=${!name}"; fi
done
echo "${line}" >> "${STUB_LOG}"
[[ "${LEGO_SYMBOL_OVERRIDE}" != "${STUB_FAIL_SYMBOL:-}" ]] || { echo "stub: ${LEGO_SYMBOL_OVERRIDE} fails" >&2; exit 7; }
""".replace("@RECORDED@", " ".join(RECORDED))
CLOCK = ('import os\n\n\ndef is_regular_session(at=None):\n'
         '    return os.environ.get("FAKE_SESSION") == "1"\n')
EXPECTED = {
    "QBTS": {"LEGO_SYMBOL_OVERRIDE": "QBTS", "LEGO_FIX_C_OVERRIDE": "1500", "LEGO_DIFF_OVERRIDE": "50",
             "LEGO_LEAPS_OVERRIDE": "100", "LEGO_ALLOW_FRACTIONAL_OVERRIDE": "true",
             "LEGO_LEAPS_CONFIRMED": "yes"},
    "RGTI": {"LEGO_SYMBOL_OVERRIDE": "RGTI", "LEGO_FIX_C_OVERRIDE": "1500", "LEGO_DIFF_OVERRIDE": "50",
             "LEGO_LEAPS_OVERRIDE": "100", "LEGO_ALLOW_FRACTIONAL_OVERRIDE": "true",
             "LEGO_BOT_OVERRIDE": "rgti", "LEGO_LEAPS_CONFIRMED": "yes"},
    "USAR": {"LEGO_SYMBOL_OVERRIDE": "USAR", "LEGO_FIX_C_OVERRIDE": "1500", "LEGO_DIFF_OVERRIDE": "50",
             "LEGO_LEAPS_OVERRIDE": "100", "LEGO_ALLOW_FRACTIONAL_OVERRIDE": "true",
             "LEGO_BOT_OVERRIDE": "usar", "LEGO_LEAPS_CONFIRMED": "yes"},
}


@pytest.fixture()
def stubbed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / SCRIPT, repo / SCRIPT)
    (repo / "deploy" / "cloudshell-prod-live.sh").write_text(STUB)
    (repo / "market_clock.py").write_text(CLOCK)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "sandbox"], cwd=repo, check=True)
    return {"repo": repo, "log": tmp_path / "stub.log"}


def run(stubbed, *args, **env):
    stubbed["log"].write_text("")
    process_env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(stubbed["repo"]),
                   "STUB_LOG": str(stubbed["log"]), "PYTHONDONTWRITEBYTECODE": "1",
                   "LEGO_ALERTS_CONFIRMED": "yes", **env}
    process_env = {k: v for k, v in process_env.items() if v is not None}
    return subprocess.run(["bash", SCRIPT, *args], cwd=stubbed["repo"], env=process_env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)


def calls(stubbed):
    """One dict per bot the wrapper ran, in order."""
    return [dict(item.split("=", 1) for item in line.split()[1:])
            for line in stubbed["log"].read_text().splitlines() if line.startswith("CALL")]


def table():
    """The BOTS rows of the committed script."""
    text = (ROOT / SCRIPT).read_text(encoding="utf-8")
    body = re.search(r"readonly BOTS=\(\n(.*?)\n\)", text, re.S).group(1)
    return [re.fullmatch(r'\s*"(.*)"', line).group(1).split() for line in body.splitlines()]


# ------------------------------------------------------------------------- the table
def test_the_table_names_each_bots_function_as_the_inventory_does():
    rows = table()
    assert [row[0] for row in rows] == ["QBTS", "RGTI", "USAR"]
    functions = [function_name("PROD", None if row[5] == "-" else row[5]) for row in rows]
    assert functions == ["lego-tick-prod", "lego-tick-prod-rgti", "lego-tick-prod-usar"]
    assert len({row[0] for row in rows}) == len(rows) == len(set(functions))


def test_the_script_only_drives_the_prefunded_canary():
    text = (ROOT / SCRIPT).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "initial-funding" not in code and "LEGO_FUNDING_MODE_OVERRIDE=" not in code
    assert "LEGO_RETIRE_SYMBOL_OVERRIDE=" not in code
    assert "exec bash deploy/cloudshell-prod-live.sh" in code


# ----------------------------------------------------------------------- the run
def test_every_bot_gets_exactly_its_row(stubbed):
    result = run(stubbed)
    assert result.returncode == 0, result.stderr
    recorded = calls(stubbed)
    assert [c["LEGO_SYMBOL_OVERRIDE"] for c in recorded] == ["QBTS", "RGTI", "USAR"]
    for call in recorded:
        expected = {**EXPECTED[call["LEGO_SYMBOL_OVERRIDE"]], "LEGO_ALERTS_CONFIRMED": "yes"}
        assert call == expected
    assert "Every bot asked for is deployed" in result.stdout
    for symbol, target in (("QBTS", "lego-tick-prod "), ("RGTI", "lego-tick-prod-rgti"),
                           ("USAR", "lego-tick-prod-usar")):
        assert re.search(rf"{symbol}\s+{target}\s+deployed", result.stdout)


def test_nothing_an_earlier_command_left_in_the_shell_reaches_a_bot(stubbed):
    result = run(stubbed, LEGO_FUNDING_MODE_OVERRIDE="initial-funding",
                 LEGO_RETIRE_SYMBOL_OVERRIDE="QBTS", LEGO_BOT_OVERRIDE="usar",
                 LEGO_SYMBOL_OVERRIDE="UBER", LEGO_FIX_C_OVERRIDE="99999",
                 LEGO_MAX_ORDER_QUANTITY_OVERRIDE="5000", LEGO_LEAPS_CONFIRMED="yes",
                 LEGO_RELEASE_AUTHORIZATION_OVERRIDE="0" * 64, EXPECTED_CANDIDATE_HASH="1" * 64)
    assert result.returncode == 0, result.stderr
    for call in calls(stubbed):
        assert call == {**EXPECTED[call["LEGO_SYMBOL_OVERRIDE"]], "LEGO_ALERTS_CONFIRMED": "yes"}


def test_a_row_without_the_confirmation_never_takes_the_shells(stubbed):
    script = stubbed["repo"] / SCRIPT
    script.write_text(script.read_text(encoding="utf-8").replace(
        '    "RGTI  1500  50  100  true  rgti  yes"', '    "RGTI  1500  50  100  true  rgti  no"'))
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "no"],
                   cwd=stubbed["repo"], check=True)
    result = run(stubbed, LEGO_LEAPS_CONFIRMED="yes")
    assert result.returncode == 0, result.stderr
    confirmed = {c["LEGO_SYMBOL_OVERRIDE"]: c.get("LEGO_LEAPS_CONFIRMED") for c in calls(stubbed)}
    assert confirmed == {"QBTS": "yes", "RGTI": None, "USAR": "yes"}


def test_the_account_wide_values_reach_every_bot(stubbed):
    result = run(stubbed, LEGO_FEE_PCT_OVERRIDE="0.35", ALERT_WEBHOOK_SECRET_OVERRIDE="lego-alert-webhook",
                 LEGO_PROD_DB_INSTANCE="lego-firebase-prod")
    assert result.returncode == 0, result.stderr
    recorded = calls(stubbed)
    assert len(recorded) == 3
    for call in recorded:
        assert call["LEGO_FEE_PCT_OVERRIDE"] == "0.35"
        assert call["ALERT_WEBHOOK_SECRET_OVERRIDE"] == "lego-alert-webhook"
        assert call["LEGO_PROD_DB_INSTANCE"] == "lego-firebase-prod"


def test_only_the_bots_named_are_deployed_in_the_order_of_the_table(stubbed):
    result = run(stubbed, "USAR", "QBTS")
    assert result.returncode == 0, result.stderr
    assert [c["LEGO_SYMBOL_OVERRIDE"] for c in calls(stubbed)] == ["QBTS", "USAR"]


@pytest.mark.parametrize("wanted", ["UBER", "qbts", ""])
def test_a_symbol_outside_the_table_is_refused_before_any_bot(stubbed, wanted):
    result = run(stubbed, "QBTS", wanted)
    assert result.returncode == 1
    assert "is not a symbol of BOTS: QBTS RGTI USAR" in result.stderr
    assert calls(stubbed) == []


def test_a_failed_bot_keeps_its_release_and_the_next_bot_still_goes(stubbed):
    result = run(stubbed, LEGO_FEE_PCT_OVERRIDE="0.35", STUB_FAIL_SYMBOL="RGTI")
    assert result.returncode == 1
    assert [c["LEGO_SYMBOL_OVERRIDE"] for c in calls(stubbed)] == ["QBTS", "RGTI", "USAR"]
    assert re.search(r"QBTS\s+lego-tick-prod\s+deployed", result.stdout)
    assert re.search(r"RGTI\s+lego-tick-prod-rgti\s+FAILED \(status 7\)", result.stdout)
    assert re.search(r"USAR\s+lego-tick-prod-usar\s+deployed", result.stdout)
    assert ("LEGO_ALERTS_CONFIRMED=yes LEGO_FEE_PCT_OVERRIDE=0.35 "
            "bash deploy/cloudshell-prod-live-all.sh RGTI") in result.stdout


# ------------------------------------------------------- the gates every bot would fail alike
def test_alerts_must_be_confirmed_before_any_bot(stubbed):
    result = run(stubbed, LEGO_ALERTS_CONFIRMED=None)
    assert result.returncode == 1 and "LEGO_ALERTS_CONFIRMED=yes" in result.stderr
    assert calls(stubbed) == []


def test_a_dirty_checkout_is_refused_before_any_bot(stubbed):
    (stubbed["repo"] / "bots.txt").write_text("USAR 9999\n")
    result = run(stubbed)
    assert result.returncode == 1 and "clean, committed checkout" in result.stderr
    assert calls(stubbed) == []


def test_the_regular_session_is_refused_before_any_bot(stubbed):
    result = run(stubbed, FAKE_SESSION="1")
    assert result.returncode == 1 and "regular session" in result.stderr
    assert calls(stubbed) == []


def test_sdk_debug_logging_is_refused_before_any_bot(stubbed):
    result = run(stubbed, WEBULL_API_DEBUG="1")
    assert result.returncode == 1 and "WEBULL_API_DEBUG" in result.stderr
    assert calls(stubbed) == []


@pytest.mark.parametrize("row, message", [
    ("QBTS  1500  50  100  true  -", "needs 7 values"),
    ("qbts  1500  50  100  true  -  no", "upper-case ticker"),
    ("QBTS  1,500  50  100  true  -  no", "FIX_C must be a number > 0"),
    ("QBTS  1500  0  100  true  -  no", "DIFF must be a number > 0"),
    ("QBTS  1500  50  100  yes  -  no", "FRACTIONAL must be true or false"),
    ("QBTS  1500  50  100  true  Usar  no", "BOT must be -"),
    ("QBTS  1500  50  100  true  -  maybe", "LEAPS_CONFIRMED must be yes or no"),
    ("RGTI  1500  50  100  true  -  no", "BOTS lists RGTI twice"),
    ("UBER  1500  50  100  true  usar  no", "BOTS sends two symbols to lego-tick-prod-usar"),
])
def test_a_bad_row_stops_the_whole_run_before_any_bot(stubbed, row, message):
    script = stubbed["repo"] / SCRIPT
    text = script.read_text(encoding="utf-8")
    if "twice" in message or "two symbols" in message:
        text = text.replace('    "USAR  1500  50  100  true  usar  yes"\n',
                            f'    "USAR  1500  50  100  true  usar  yes"\n    "{row}"\n')
    else:
        text = text.replace('    "QBTS  1500  50  100  true  -     yes"', f'    "{row}"')
    script.write_text(text)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "row"],
                   cwd=stubbed["repo"], check=True)
    result = run(stubbed)
    assert result.returncode == 1 and message in result.stderr, result.stderr
    assert calls(stubbed) == []


# ---------------------------------------------- end to end, over the real single-bot script
RUNNING = {"lego-tick-prod": bot_env("QBTS"), "lego-tick-prod-rgti": bot_env("RGTI"),
           "lego-tick-prod-usar": bot_env("USAR")}


def test_each_running_bot_is_redeployed_to_its_own_function_with_the_new_caps(sandbox, tmp_path):
    repo = sandbox["repo"]
    shutil.copy(ROOT / SCRIPT, repo / SCRIPT)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", SCRIPT], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "wrapper"], cwd=repo, check=True)
    env = {"LEGO_ALERTS_CONFIRMED": "yes", "LEGO_FEE_PCT_OVERRIDE": "0.35",
           "ALERT_WEBHOOK_SECRET_OVERRIDE": "lego-alert-webhook-prod",
           "LEGO_SYMBOL_OVERRIDE": "UBER", "LEGO_FUNDING_MODE_OVERRIDE": "initial-funding",
           "STUB_RTDB_INSTANCES": PROD_INSTANCES, "STUB_TOKEN_STATUS": "fresh",
           "STUB_PREFLIGHT_JSON": json.dumps(PREFLIGHT_OK), **services(tmp_path, RUNNING)}
    result = run_script(sandbox, SCRIPT, env)
    recorded = log(sandbox)
    # The stub gcloud stops every bot at `functions deploy` (78), so each one reads FAILED.
    assert result.returncode == 1, result.stdout[-3000:] + result.stderr
    assert result.stdout.count("FAILED (status 78)") == 3, result.stdout[-3000:]
    deploys = re.findall(r"gcloud functions deploy (\S+) .*", recorded)
    assert deploys == ["lego-tick-prod", "lego-tick-prod-rgti", "lego-tick-prod-usar"]
    for target, symbol in zip(deploys, ("QBTS", "RGTI", "USAR")):
        line = re.search(rf"gcloud functions deploy {target} .*", recorded).group(0)
        for part in (f"LEGO_SYMBOL={symbol},", "WEBULL_ENV=PROD,", "LEGO_MODE=trade,",
                     "LEGO_FIX_C=1500,", "LEGO_DIFF=50,", "LEGO_LEAPS=100,",
                     "LEGO_MAX_ORDER_NOTIONAL_USD=150,", "LEGO_MAX_SESSION_BUY_ORDERS=3,"):
            assert part in line, (target, part)
        assert "LEGO_PROD_LIVE_ACK" not in line
    preflights = [line for line in recorded.splitlines()
                  if line.startswith("python3 tools/prod_live_preflight.py")]
    assert ["--leaps-confirmed" in line for line in preflights] == [True, True, True]
    assert [re.search(r"--symbol (\S+)", line).group(1) for line in preflights] == ["QBTS", "RGTI", "USAR"]
    assert all("--funding prefunded" in line and "--window-sessions 30" in line for line in preflights)
    assert ("--reserve lego-tick-prod-rgti:RGTI:1500:150" in preflights[0]
            and "--reserve lego-tick-prod-usar:USAR:1500:150" in preflights[0])
