"""A second bot (USAR) beside the QBTS one, on the same Webull account (2026-11-03, second-bot addendum).

Every deploy went to the one function of its environment (lego-tick-prod). The owner's command
for a USAR bot would therefore have replaced the QBTS bot, leaving its 48.66 real shares and any
order in flight with no bot to manage them, and nothing refused it. Now:

* LEGO_BOT_OVERRIDE=<name> deploys to a function of its own (lego-tick-prod-<name>): the same
  account, token, database and service accounts; without it nothing changes.
* tools/bot_inventory.py reads the LEGO functions of the project before any PROD deploy and
  refuses, fail-closed: a symbol or database switch on a function that holds a real-money chain
  (unless the owner names the symbol retired), one symbol in two functions, and an inventory it
  cannot read.
* the account preflight keeps the cash of the other real-money bots (FIX_C + 1 % each) out of
  the buying power this release may use.

Chains, fences, circuits and the kill switch were already keyed by symbol (lego_state.chain_key,
lego_outbox.account_symbol_fence_key); the runtime is unchanged.

Nothing here touches a network, a broker or Secret Manager.
"""
from __future__ import annotations

import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from test_audit_20261103 import (EXACT, PREFLIGHT_OK, log, observe_env, run_live, run_script,
                                 sandbox)  # noqa: F401  (sandbox: fixture)
from test_deploy_scripts import PROD_DB
from test_prod_live_preflight import call_leg, facts, finding, option, params
from tools import bot_inventory as bi
from tools import prod_live_preflight as pf

ROOT = Path(__file__).parent
PAPER_DB = "https://lego-firebase-paper.asia-southeast1.firebasedatabase.app"


def bot_env(symbol, *, mode="trade", active="true", db=PROD_DB, fix_c="1500", cap="150"):
    return {"WEBULL_ENV": "PROD", "FIREBASE_DB_URL": db, "LEGO_SYMBOL": symbol, "LEGO_FIX_C": fix_c,
            "LEGO_DIFF": "50", "LEGO_LEAPS": "100", "LEGO_MODE": mode, "LEGO_ACTIVE": active,
            "LEGO_MAX_ORDER_NOTIONAL_USD": cap}


QBTS_LIVE = {"lego-tick-prod": bot_env("QBTS")}


def judge(functions, *, target="lego-tick-prod-usar", symbol="USAR", db=PROD_DB, retire=None):
    return bi.judge(functions, target=target, symbol=symbol, database_url=db, retire=retire)


def message(report, id_):
    return next(f["message"] for f in report["findings"] if f["id"] == id_)


# ------------------------------------------------------------------ names
def test_the_default_function_keeps_its_name_and_a_bot_gets_its_own():
    assert bi.function_name("PROD") == "lego-tick-prod"
    assert bi.function_name("PROD", "usar") == "lego-tick-prod-usar"
    assert bi.function_name("UAT", "usar") == "lego-tick-uat-usar"


@pytest.mark.parametrize("bot", ["USAR", "usar-1", "1usar", "us_ar", "a" * 17, "usar "])
def test_a_bot_name_is_short_lower_case_letters_and_digits(bot):
    with pytest.raises(ValueError):
        bi.function_name("PROD", bot)


def test_the_suggested_bot_name_comes_from_the_symbol():
    assert bi.suggested_bot("USAR") == "usar"
    assert bi.suggested_bot("BRK.B") == "brkb"


def test_only_the_lego_functions_of_the_environment_are_inventoried():
    listed = ["lego-tick-prod", "lego-tick-prod-usar", "lego-tick-uat", "lego-tick-prod-USAR",
              "lego-tick-production", "lego-tick-prod-", "other-service", "lego-tick-prod-usar-x"]
    assert bi.lego_function_names(listed, "PROD") == ["lego-tick-prod", "lego-tick-prod-usar"]


@pytest.mark.parametrize("url,real", [
    (PROD_DB, True), (PAPER_DB, False), ("https://example-default-rtdb.firebaseio.com", True),
    ("", True), (None, True)])                      # unknown is real money: fail closed
def test_only_a_database_named_for_paper_holds_no_real_money(url, real):
    assert bi.real_money_database(url) is real


# ------------------------------------------------------------------ the judgement
def test_the_owners_command_would_have_replaced_the_qbts_bot_and_is_refused():
    report = judge(QBTS_LIVE, target="lego-tick-prod")
    assert report["ok"] is False and report["blocking"] == ["symbol_switch"]
    text = message(report, "symbol_switch")
    for part in ("lego-tick-prod runs QBTS", "unmanaged", "LEGO_BOT_OVERRIDE=usar",
                 "lego-tick-prod-usar", "LEGO_RETIRE_SYMBOL_OVERRIDE=QBTS"):
        assert part in text, part


def test_a_usar_bot_beside_qbts_is_allowed_and_names_the_cash_qbts_keeps():
    report = judge(QBTS_LIVE)
    assert report["ok"] is True, report["findings"]
    assert report["target"] == "lego-tick-prod-usar" and report["target_exists"] is False
    assert report["reserve"] == ["lego-tick-prod:QBTS:1500:150"]
    assert report["bots"] == [{"function": "lego-tick-prod", "symbol": "QBTS", "mode": "trade",
                               "active": "true", "fix_c": "1500", "database": "real",
                               "label": "lego-tick-prod: QBTS trade/active, real money"}]


def test_redeploying_a_bot_keeps_it_and_reserves_the_other_one():
    both = {**QBTS_LIVE, "lego-tick-prod-usar": bot_env("USAR")}
    assert judge(both, target="lego-tick-prod", symbol="QBTS")["reserve"] == ["lego-tick-prod-usar:USAR:1500:150"]
    again = judge(both)
    assert again["ok"] is True and again["target_exists"] is True
    assert again["reserve"] == ["lego-tick-prod:QBTS:1500:150"]                 # never itself


def test_a_paper_function_may_change_symbol_as_qbts_replaced_paper_uber():
    paper = {"lego-tick-prod": bot_env("UBER", mode="paper", db=PAPER_DB, fix_c="3000")}
    report = judge(paper, target="lego-tick-prod", symbol="QBTS")
    assert report["ok"] is True and report["reserve"] == []


def test_a_real_money_function_may_not_move_to_another_database():
    report = judge(QBTS_LIVE, target="lego-tick-prod", symbol="QBTS", db=PAPER_DB)
    assert report["blocking"] == ["symbol_switch"]
    assert "database" in message(report, "symbol_switch")


def test_an_observe_function_on_the_real_database_is_protected_as_well():
    observing = {"lego-tick-prod": bot_env("QBTS", mode="observe", active="false")}
    assert judge(observing, target="lego-tick-prod")["blocking"] == ["symbol_switch"]


def test_retiring_a_symbol_needs_its_exact_name():
    retired = judge(QBTS_LIVE, target="lego-tick-prod", retire="QBTS")
    assert retired["ok"] is True
    assert "no longer managed" in message(retired, "symbol_switch")
    assert judge(QBTS_LIVE, target="lego-tick-prod", retire="qbts")["ok"] is True
    assert judge(QBTS_LIVE, target="lego-tick-prod", retire="UBER")["blocking"] == ["symbol_switch", "retire"]
    # Naming a symbol that this deploy does not leave is refused too: the confirmation must mean it.
    assert judge(QBTS_LIVE, retire="QBTS")["blocking"] == ["retire"]
    assert judge(QBTS_LIVE, target="lego-tick-prod", symbol="QBTS", retire="QBTS")["blocking"] == ["retire"]


@pytest.mark.parametrize("other", [bot_env("QBTS"), bot_env("QBTS", mode="observe", active="false")])
def test_one_symbol_never_runs_in_two_functions(other):
    report = judge({"lego-tick-prod": other}, target="lego-tick-prod-qbts", symbol="QBTS")
    assert report["blocking"] == ["symbol_twice"]
    assert "lego-tick-prod already runs QBTS" in message(report, "symbol_twice")


def test_a_paper_bot_may_run_a_symbol_the_real_bot_trades():
    report = judge(QBTS_LIVE, target="lego-tick-prod-qbtspaper", symbol="QBTS", db=PAPER_DB)
    assert report["ok"] is True, report["findings"]


def test_only_real_money_bots_keep_cash():
    functions = {
        "lego-tick-prod": bot_env("QBTS"),
        "lego-tick-prod-uber": bot_env("UBER", mode="paper", db=PAPER_DB, fix_c="3000"),
        "lego-tick-prod-tsla": bot_env("TSLA", mode="observe", active="false", fix_c="2000"),
        "lego-tick-prod-nvda": bot_env("NVDA", mode="preview", fix_c="4000"),
        "lego-tick-prod-amd": bot_env("AMD", mode="trade", active="false", fix_c="5000"),
    }
    assert judge(functions)["reserve"] == ["lego-tick-prod:QBTS:1500:150"]


@pytest.mark.parametrize("fix_c,cap", [("", "150"), ("abc", "150"), ("-5", "150"), ("0", "150"),
                                       ("NaN", "150"), ("1500", ""), ("1500", "0"), ("1500", "x")])
def test_a_real_money_bot_whose_principal_or_order_cap_cannot_be_read_blocks(fix_c, cap):
    report = judge({"lego-tick-prod": bot_env("QBTS", fix_c=fix_c, cap=cap)})
    assert "inventory_unreadable" in report["blocking"]


# ------------------------------------------------------------------ reading the project
class Gcloud:
    """subprocess.run of gcloud: answers from a dict of Cloud Run services."""

    def __init__(self, services, *, fail=()):
        self.services, self.fail, self.calls = services, set(fail), []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        words = argv[1:4]
        if tuple(words[:3]) == ("run", "services", "list"):
            if "list" in self.fail:
                return _done(1, "")
            return _done(0, "\n".join([*self.services, "unrelated-service"]) + "\n")
        if tuple(words[:3]) == ("run", "services", "describe"):
            name = argv[4]
            if name in self.fail or name not in self.services:
                return _done(1, "")
            env = [{"name": k, "value": v} for k, v in self.services[name].items()]
            env.append({"name": "WEBULL_APP_KEY", "valueFrom": {"secretKeyRef": {"name": "webull-app-key-prod"}}})
            return _done(0, json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}))
        raise AssertionError(f"unexpected call {argv}")


def _done(code, out):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def test_the_project_is_read_from_its_cloud_run_services():
    gcloud = Gcloud({"lego-tick-prod": bot_env("QBTS"), "lego-tick-uat": bot_env("AAPL")})
    functions = bi.read_functions("lego-firebase", "asia-southeast1", "PROD", runner=gcloud)
    assert list(functions) == ["lego-tick-prod"] and functions["lego-tick-prod"]["LEGO_SYMBOL"] == "QBTS"
    assert functions["lego-tick-prod"]["WEBULL_APP_KEY"] is None             # a secret is never read
    assert gcloud.calls[0] == ["gcloud", "run", "services", "list", "--region=asia-southeast1",
                               "--project=lego-firebase", "--format=value(metadata.name)"]


@pytest.mark.parametrize("fail", ["list", "lego-tick-prod"])
def test_an_inventory_that_cannot_be_read_blocks(monkeypatch, capsys, fail):
    gcloud = Gcloud(QBTS_LIVE, fail=[fail])
    monkeypatch.setattr(bi.subprocess, "run", gcloud)
    code = bi.main(["--project", "lego-firebase", "--region", "asia-southeast1", "--env", "PROD",
                    "--target", "lego-tick-prod-usar", "--symbol", "USAR", "--database-url", PROD_DB])
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert code == 1 and report["blocking"] == ["inventory_unreadable"]
    assert "[BLOCK] inventory_unreadable" in out.err


def test_the_cli_reports_the_bots_and_the_cash_they_keep(monkeypatch, capsys):
    monkeypatch.setattr(bi.subprocess, "run", Gcloud(QBTS_LIVE))
    code = bi.main(["--project", "lego-firebase", "--region", "asia-southeast1", "--env", "PROD",
                    "--target", "lego-tick-prod-usar", "--symbol", "USAR", "--database-url", PROD_DB])
    out = capsys.readouterr()
    assert code == 0 and json.loads(out.out)["reserve"] == ["lego-tick-prod:QBTS:1500:150"]
    assert "lego-tick-prod: QBTS trade/active, real money" in out.err


@pytest.mark.parametrize("argv", [
    ["--target", "lego-tick-uat-usar"],                    # another environment's function
    ["--target", "lego-tick-prod-USAR"], ["--symbol", "usar;rm"]])
def test_malformed_arguments_block(monkeypatch, capsys, argv):
    monkeypatch.setattr(bi.subprocess, "run", Gcloud(QBTS_LIVE))
    base = {"--project": "lego-firebase", "--region": "asia-southeast1", "--env": "PROD",
            "--target": "lego-tick-prod-usar", "--symbol": "USAR", "--database-url": PROD_DB}
    base.update(dict(zip(argv[::2], argv[1::2])))
    code = bi.main([part for pair in base.items() for part in pair])
    assert code == 1 and json.loads(capsys.readouterr().out)["blocking"] == ["arguments"]


# ------------------------------------------------------------------ the preflight keeps their cash
# Owner's rule 2026-11-04: the least that opens the next order. This release's first BUY (or one
# order at its cap) plus one order at the cap of every other bot trading real money, all + 1 %.
# FIX_C + 1 % of every bot is a WARN (buying_power_principal), no longer a BLOCK.
QBTS_KEPT = (("lego-tick-prod", "QBTS", Decimal("1500"), Decimal("150")),)


def usar_facts(**overrides):
    return facts(positions=[option(symbol="USAR", legs=[call_leg(symbol="USAR")])],
                 quote={"price": "10.51", "quote_time": None}, **overrides)


def test_the_preflight_needs_the_next_order_of_this_bot_and_one_of_each_other():
    # USAR flat at 10.51 with 100 LEAPS: first BUY 449.00 -> 453.49; QBTS one order 150 -> 151.50.
    blocked = pf.judge(usar_facts(buying_power_usd="604.98"),
                       params(symbol="USAR", funding="initial-funding", reserve=QBTS_KEPT))
    assert blocked["blocking"] == ["buying_power"]
    text = finding(blocked, "buying_power")["message"]
    for part in ("604.98", "604.99", "453.49 for this release (its first BUY of 449.00 USD + 1 %)",
                 "151.50 for one order of each other real-money bot (lego-tick-prod QBTS 151.50)"):
        assert part in text, part
    derived = blocked["derived"]
    assert (derived["required_cash_usd"], derived["own_cash_usd"], derived["reserved_usd"],
            derived["principal_cash_usd"]) == ("604.99", "453.49", "151.50", "3030.00")
    assert derived["reserved"] == [{"function": "lego-tick-prod", "symbol": "QBTS", "usd": "151.50"}]
    # The owner's account on 4 Nov: 2218.31 opens it; below FIX_C + 1 % of both bots, so a WARN.
    passed = pf.judge(usar_facts(buying_power_usd="2218.31"),
                      params(symbol="USAR", funding="initial-funding", reserve=QBTS_KEPT))
    assert passed["ok"] is True, passed["findings"]
    assert finding(passed, "buying_power")["severity"] == "INFO"
    warn = finding(passed, "buying_power_principal")
    assert warn["severity"] == "WARN" and "3030.00" in warn["message"] and "NOT_PLACED" in warn["message"]
    rich = pf.judge(usar_facts(buying_power_usd="3030.00"),
                    params(symbol="USAR", funding="initial-funding", reserve=QBTS_KEPT))
    assert "buying_power_principal" not in {f["id"] for f in rich["findings"]}


def test_a_canary_with_no_first_order_needs_one_order_at_its_cap():
    usar_kept = (("lego-tick-prod-usar", "USAR", Decimal("1500"), Decimal("150")),)
    report = pf.judge(facts(buying_power_usd="303.00"), params(reserve=usar_kept))   # 38 real: none
    assert report["derived"]["first_order"] is None and report["ok"] is True, report["findings"]
    assert report["derived"]["required_cash_usd"] == "303.00"
    assert "one order at its 150 USD cap" in finding(report, "buying_power")["message"]
    assert pf.judge(facts(buying_power_usd="302.99"), params(reserve=usar_kept))["blocking"] == ["buying_power"]


def test_without_other_bots_only_this_release_counts():
    report = pf.judge(facts(), params())
    assert report["derived"]["required_cash_usd"] == "151.50"
    assert finding(report, "buying_power")["message"] == (
        "USD buying power 1600.00 >= the 151.50 that opens the next order: 151.50 for this release "
        "(one order at its 150 USD cap + 1 %)")
    assert not {"reserved_usd", "reserved"} & set(report["derived"])


def test_the_preflight_cli_reads_the_reserve(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(pf, "run", lambda p: seen.setdefault("params", p) and pf._report({}, [], {}))
    argv = ["--symbol", "USAR", "--fix-c", "1500", "--diff", "50", "--leaps", "100",
            "--allow-fractional", "true", "--window-sessions", "2", "--funding", "initial-funding"]
    assert pf.main(argv + ["--reserve", "lego-tick-prod:QBTS:1500:150"]) == 0
    assert seen["params"].reserve == QBTS_KEPT
    capsys.readouterr()
    for bad in ("lego-tick-prod:QBTS:1500", "lego-tick-prod:QBTS:-1:150", "lego-tick-prod:QBTS:1500:0",
                "x:QBTS:1500:150", "lego-tick-prod:q;b:1500:150"):
        assert pf.main(argv + ["--reserve", bad]) == 1
        assert json.loads(capsys.readouterr().out)["blocking"] == ["arguments"]


# ------------------------------------------------------------------ the deploy scripts (sandbox)
def services(tmp_path, functions):
    """The stub gcloud lists these Cloud Run services and describes them (and their functions)."""
    folder = tmp_path / "services"
    folder.mkdir(exist_ok=True)
    for name, env in functions.items():
        variables = [{"name": k, "value": v} for k, v in env.items()]
        (folder / f"{name}.json").write_text(json.dumps(
            {"spec": {"template": {"spec": {"containers": [{"env": variables}]}}}}))
        (folder / f"function-{name}.json").write_text(json.dumps(
            {"serviceConfig": {"environmentVariables": env}}))
    return {"STUB_RUN_SERVICES": " ".join([*functions, "unrelated-service"]),
            "STUB_SERVICES_DIR": str(folder)}


USAR = {**EXACT, "LEGO_SYMBOL_OVERRIDE": "USAR", "LEGO_FUNDING_MODE_OVERRIDE": "initial-funding",
        "LEGO_LEAPS_CONFIRMED": "yes"}
USAR_PREFLIGHT = {**PREFLIGHT_OK, "warnings": ["leaps_held"], "derived": {
    **PREFLIGHT_OK["derived"], "reference_price": "10.51", "real_shares": "0", "leaps_held": "0",
    "real_target": "42.72", "funding": "initial-funding", "required_cash_usd": "604.99",
    "buying_power_usd": "2218.31", "own_cash_usd": "453.49", "reserved_usd": "151.50",
    "principal_cash_usd": "3030.00",
    "reserved": [{"function": "lego-tick-prod", "symbol": "QBTS", "usd": "151.50"}],
    "first_order": {"side": "BUY", "notional_usd": "449.00", "quantity": "42.72"}}}
USAR_ACK = re.compile(
    r"LEGO_PROD_LIVE_ACK=(LIVE-PROD-USAR-q\d+-n1875-o2-\d{8}T\d{4}Z-initial-funding-[0-9a-f]{16})")


def preflight_call(sandbox):
    return next(line for line in log(sandbox).splitlines() if "tools/prod_live_preflight.py" in line)


def test_the_owners_usar_command_without_a_bot_is_refused_before_anything_is_planned(sandbox, tmp_path):
    result = run_live(sandbox, {**USAR, **services(tmp_path, QBTS_LIVE)}, preflight=USAR_PREFLIGHT)
    assert result.returncode == 1, result.stdout[-3000:] + result.stderr
    assert "[BLOCK] symbol_switch" in result.stderr and "LEGO_BOT_OVERRIDE=usar" in result.stderr
    recorded = log(sandbox)
    assert "prod_live_preflight" not in recorded and "functions deploy" not in recorded
    assert "LEGO_PROD_LIVE_ACK=" not in result.stdout


def test_a_usar_bot_stops_at_its_own_acknowledgement(sandbox, tmp_path):
    env = {**USAR, "LEGO_BOT_OVERRIDE": "usar", **services(tmp_path, QBTS_LIVE)}
    result = run_live(sandbox, env, preflight=USAR_PREFLIGHT)
    assert result.returncode == 3, result.stdout[-3000:] + result.stderr
    assert USAR_ACK.search(result.stdout), result.stdout[-3000:]
    assert "functions deploy" not in log(sandbox)
    call = preflight_call(sandbox)
    assert "--symbol USAR" in call and "--reserve lego-tick-prod:QBTS:1500:150" in call
    for text in ("lego-tick-prod-usar (new, LEGO_BOT_OVERRIDE=usar)",
                 "lego-tick-prod: QBTS trade/active, real money",
                 "2218.31 / 604.99 USD (this bot 453.49 + one order of other bots 151.50)",
                 "3030.00 USD: NOT covered",
                 "LEGO_BOT_OVERRIDE=usar LEGO_ALLOW_FRACTIONAL_OVERRIDE=true LEGO_SYMBOL_OVERRIDE=USAR",
                 "WEBULL_ENV=PROD LEGO_SYMBOL=USAR"):
        assert text in result.stdout, text


def test_the_acknowledged_usar_bot_deploys_to_its_own_function(sandbox, tmp_path):
    env = {**USAR, "LEGO_BOT_OVERRIDE": "usar", **services(tmp_path, QBTS_LIVE)}
    ack = USAR_ACK.search(run_live(sandbox, env, preflight=USAR_PREFLIGHT).stdout).group(1)
    result = run_live(sandbox, {**env, "LEGO_PROD_LIVE_ACK": ack}, preflight=USAR_PREFLIGHT)
    assert result.returncode == 78, result.stdout[-3000:] + result.stderr   # stub stops at deploy
    recorded = log(sandbox)
    deployed = re.search(r"gcloud functions deploy lego-tick-prod-usar .*", recorded).group(0)
    assert "gcloud functions deploy lego-tick-prod " not in recorded            # QBTS untouched
    for value in ("LEGO_SYMBOL=USAR,LEGO_FIX_C=1500,", "LEGO_MODE=trade,LEGO_ACTIVE=true,",
                  f"LEGO_PROD_LIVE_ACK={ack}", f"FIREBASE_DB_URL={PROD_DB},",
                  "--service-account=lego-runtime-prod@lego-firebase.iam.gserviceaccount.com"):
        assert value in deployed, value
    assert "gcloud scheduler jobs describe lego-tick-prod-usar " in recorded


def test_the_default_release_has_no_bot_and_keeps_nothing_without_other_bots(sandbox):
    result = run_live(sandbox)
    assert result.returncode == 3, result.stdout[-3000:] + result.stderr
    assert "--reserve" not in preflight_call(sandbox)
    assert "lego-tick-prod (new)" in result.stdout and "Other LEGO bots" in result.stdout
    assert "LEGO_BOT_OVERRIDE" not in result.stdout


@pytest.mark.parametrize("script", ["deploy/cloudshell-prod-live.sh", "deploy/cloudshell-all-in-one.sh"])
@pytest.mark.parametrize("bot", ["USAR", "usar-1", "1usar"])
def test_a_malformed_bot_name_is_refused_before_any_cloud_call(sandbox, script, bot):
    env = ({**USAR, "LEGO_BOT_OVERRIDE": bot} if script.endswith("live.sh")
           else {**observe_env(), "LEGO_BOT_OVERRIDE": bot})
    result = (run_live(sandbox, env) if script.endswith("live.sh") else run_script(sandbox, script, env))
    assert result.returncode == 1 and "LEGO_BOT_OVERRIDE" in result.stderr
    assert log(sandbox) == ""


def test_a_malformed_retirement_is_refused_before_any_cloud_call(sandbox):
    result = run_live(sandbox, {**USAR, "LEGO_RETIRE_SYMBOL_OVERRIDE": "qbts;rm"})
    assert result.returncode == 1 and "LEGO_RETIRE_SYMBOL_OVERRIDE" in result.stderr
    assert log(sandbox) == ""


def test_the_deploy_script_itself_refuses_a_symbol_switch_on_a_real_money_function(sandbox, tmp_path):
    env = {**observe_env(), "LEGO_SYMBOL_OVERRIDE": "USAR", **services(tmp_path, QBTS_LIVE)}
    result = run_script(sandbox, "deploy/cloudshell-all-in-one.sh", env)
    assert result.returncode == 1, result.stdout[-3000:] + result.stderr
    assert "[BLOCK] symbol_switch" in result.stderr and "functions deploy" not in log(sandbox)


def test_the_deploy_script_lets_the_owner_retire_a_symbol_by_name(sandbox, tmp_path):
    env = {**observe_env(), "LEGO_SYMBOL_OVERRIDE": "USAR", "LEGO_RETIRE_SYMBOL_OVERRIDE": "QBTS",
           **services(tmp_path, QBTS_LIVE)}
    result = run_script(sandbox, "deploy/cloudshell-all-in-one.sh", env)
    assert result.returncode == 78, result.stdout[-3000:] + result.stderr   # stub stops at deploy
    assert "[WARN] symbol_switch" in result.stderr
    assert "LEGO_SYMBOL=USAR," in re.search(r"gcloud functions deploy lego-tick-prod .*", log(sandbox)).group(0)


def test_the_deploy_script_refuses_when_the_functions_cannot_be_read(sandbox):
    env = {**observe_env(), "STUB_RUN_SERVICES_FAIL": "1"}
    result = run_script(sandbox, "deploy/cloudshell-all-in-one.sh", env)
    assert result.returncode == 1 and "[BLOCK] inventory_unreadable" in result.stderr
    assert "functions deploy" not in log(sandbox)


def test_the_rollback_of_a_bot_goes_to_that_bot(sandbox, tmp_path):
    usar_live = {**QBTS_LIVE, "lego-tick-prod-usar": bot_env("USAR")}
    env = {**observe_env(), "LEGO_BOT_OVERRIDE": "usar", "LEGO_SYMBOL_OVERRIDE": "USAR",
           **services(tmp_path, usar_live)}
    result = run_script(sandbox, "deploy/cloudshell-all-in-one.sh", env)
    assert result.returncode == 78, result.stdout[-3000:] + result.stderr
    deployed = re.search(r"gcloud functions deploy lego-tick-prod-usar .*", log(sandbox)).group(0)
    assert "LEGO_SYMBOL=USAR," in deployed and "LEGO_MODE=observe,LEGO_ACTIVE=false," in deployed


def test_no_wrapper_names_the_default_function_where_a_bot_may_run():
    for script in ("deploy/cloudshell-prod-live.sh", "deploy/cloudshell-prod-observe.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        assert "lego-tick-prod" not in text, script
        assert 'FUNCTION_NAME="lego-tick-${SUFFIX}${BOT:+-${BOT}}"' in text, script
    deploy = (ROOT / "deploy/cloudshell-all-in-one.sh").read_text(encoding="utf-8")
    assert 'readonly FUNCTION_NAME="lego-tick-${SUFFIX}${BOT:+-${BOT}}"' in deploy
    assert 'readonly SCHEDULER_JOB="${FUNCTION_NAME}"' in deploy
    # One runtime and one scheduler identity for every bot: no new IAM per bot.
    assert 'readonly RUNTIME_SA_NAME="lego-runtime-${SUFFIX}"' in deploy
