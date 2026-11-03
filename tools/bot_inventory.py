"""The LEGO bots of a project, and whether a deploy may go where it asks. Reads only.

A bot is one Cloud Function gen2, a Cloud Run service: lego-tick-<env> (the first) or
lego-tick-<env>-<bot> (deploy/cloudshell-all-in-one.sh, LEGO_BOT_OVERRIDE). Every bot trades
the same Webull account, reads the same token and writes the same Realtime Database, where each
chain, fence, circuit and kill switch is keyed by its symbol (lego_state.chain_key,
lego_outbox.account_symbol_fence_key). What they share is the account, so before a PROD deploy
this refuses, fail-closed:

* symbol_switch: the function deployed to holds a real-money chain (in a database that is not
  named for paper) of another symbol, or would move to another database. Its shares and any
  order in flight would be left with no bot. Allowed only when the owner names that symbol in
  LEGO_RETIRE_SYMBOL_OVERRIDE (the bot is flat and nothing is in flight).
* symbol_twice: another function runs the symbol on a real-money database already; two bots
  would rebalance the same shares.
* retire: LEGO_RETIRE_SYMBOL_OVERRIDE names no chain this deploy would leave.
* inventory_unreadable: the functions, or the principal of a bot trading real money, cannot
  be read.

It also names the other bots trading real money (trade, active, not paper), whose principal the
account preflight keeps out of the buying power of this release (tools/prod_live_preflight.py
--reserve FUNCTION:SYMBOL:FIX_C).

    python3 tools/bot_inventory.py --project P --region R --env PROD --target lego-tick-prod-usar \\
        --symbol USAR --database-url https://... [--retire QBTS]

One JSON report on stdout, the findings on stderr; exit 0 ok, 1 blocked. Environment values of
the functions are read; secrets are bound by reference and never have a value to read.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation

SCHEMA = "lego_bot_inventory_v1"
BOT_PATTERN = r"[a-z][a-z0-9]{0,15}"
SYMBOL_PATTERN = r"[A-Z][A-Z0-9.-]{0,9}"
ENVIRONMENTS = ("UAT", "PROD")
INFO, WARN, BLOCK = "INFO", "WARN", "BLOCK"


class InventoryError(RuntimeError):
    """The functions of the project could not be read."""


def function_name(environment: str, bot: str | None = None) -> str:
    """The function a deploy goes to: the first bot's, or the named bot's own."""
    if environment not in ENVIRONMENTS:
        raise ValueError("environment must be UAT or PROD")
    base = f"lego-tick-{environment.lower()}"
    if bot is None or bot == "":
        return base
    if not re.fullmatch(BOT_PATTERN, bot):
        raise ValueError("a bot name is 1-16 lower-case letters and digits, starting with a letter")
    return f"{base}-{bot}"


def suggested_bot(symbol: str) -> str:
    name = re.sub(r"[^a-z0-9]", "", symbol.lower())[:16]
    return name if re.fullmatch(BOT_PATTERN, name) else "bot2"


def lego_function_names(names, environment: str) -> list[str]:
    pattern = re.compile(rf"lego-tick-{environment.lower()}(-{BOT_PATTERN})?")
    return sorted({name for name in names if pattern.fullmatch(name)})


def real_money_database(url) -> bool:
    """Only a database named for paper holds no real money (tools/verify_deployment.py's rule)."""
    instance = re.sub(r"^https://", "", str(url or "")).split(".")[0]
    return "paper" not in instance


def _same_database(left, right) -> bool:
    return str(left or "").strip().rstrip("/") == str(right or "").strip().rstrip("/")


def _principal(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _symbol(env: dict) -> str:
    return str(env.get("LEGO_SYMBOL") or "").strip().upper()


def _trades_real_money(env: dict) -> bool:
    return (env.get("LEGO_MODE") == "trade" and env.get("LEGO_ACTIVE") == "true"
            and real_money_database(env.get("FIREBASE_DB_URL")))


def _base(target: str) -> str:
    match = re.match(r"lego-tick-[a-z]+", target)
    return match.group(0) if match else "lego-tick-prod"


def _bot_of(function: str, base: str) -> str:
    return "no LEGO_BOT_OVERRIDE" if function == base else f"LEGO_BOT_OVERRIDE={function[len(base) + 1:]}"


def bot_summary(function: str, env: dict) -> dict:
    real = real_money_database(env.get("FIREBASE_DB_URL"))
    symbol, mode = _symbol(env), str(env.get("LEGO_MODE") or "")
    active = str(env.get("LEGO_ACTIVE") or "")
    return {"function": function, "symbol": symbol, "mode": mode, "active": active,
            "fix_c": str(env.get("LEGO_FIX_C") or ""), "database": "real" if real else "paper",
            "label": f"{function}: {symbol or '?'} {mode or '?'}/"
                     f"{'active' if active == 'true' else 'inactive'}, "
                     f"{'real money' if real else 'paper'}"}


def judge(functions: dict, *, target: str, symbol: str, database_url: str,
          retire: str | None = None) -> dict:
    """Whether a release of *symbol* may deploy to *target* with the bots *functions* hold.

    *functions* maps each LEGO function of the environment to its environment variables.
    """
    symbol = symbol.strip().upper()
    retire = (retire or "").strip().upper() or None
    base = _base(target)
    findings: list[dict] = []

    def add(id_, severity, message):
        findings.append({"id": id_, "severity": severity, "message": message})

    bots = [bot_summary(name, functions[name] or {}) for name in sorted(functions)]
    add("bots", INFO, "LEGO functions: " + ("; ".join(b["label"] for b in bots) or "none"))

    current = functions.get(target)
    retired = False
    if current is not None:
        held = _symbol(current)
        switches = held != symbol
        moves = not _same_database(current.get("FIREBASE_DB_URL"), database_url)
        if real_money_database(current.get("FIREBASE_DB_URL")) and (switches or moves):
            change = (f"deploying {symbol} there" if switches
                      else f"moving it to another database ({database_url})")
            if retire is not None and retire == held:
                retired = True
                add("symbol_switch", WARN, f"{target} holds the real-money chain of {held}; the owner "
                    f"retires it (LEGO_RETIRE_SYMBOL_OVERRIDE={held}): its shares and any order in "
                    "flight are no longer managed by any bot")
            else:
                bot = suggested_bot(symbol)
                add("symbol_switch", BLOCK, f"{target} runs {held or 'an unnamed symbol'} on the "
                    f"real-money database: {change} would leave its {held} shares and any order in "
                    f"flight unmanaged. Another symbol is another bot: LEGO_BOT_OVERRIDE={bot} "
                    f"(function {base}-{bot}). To retire {held} on purpose, flat and with nothing in "
                    f"flight: LEGO_RETIRE_SYMBOL_OVERRIDE={held}")
    if retire is not None and not retired:
        add("retire", BLOCK, f"LEGO_RETIRE_SYMBOL_OVERRIDE={retire} names no real-money chain this "
            f"deploy would leave ({target} {'runs ' + _symbol(current) if current is not None else 'is new'}); "
            "remove it")

    if real_money_database(database_url):
        for name in sorted(functions):
            env = functions[name] or {}
            if name != target and _symbol(env) == symbol and real_money_database(env.get("FIREBASE_DB_URL")):
                add("symbol_twice", BLOCK, f"{name} already runs {symbol} on the real-money database: "
                    f"a second bot of {symbol} would rebalance the same shares. Deploy {symbol} to "
                    f"{name} itself ({_bot_of(name, base)})")

    reserve = []
    for name in sorted(functions):
        env = functions[name] or {}
        if name == target or not _trades_real_money(env):
            continue
        principal, other = _principal(env.get("LEGO_FIX_C")), _symbol(env)
        if principal is None or not re.fullmatch(SYMBOL_PATTERN, other):
            add("inventory_unreadable", BLOCK, f"{name} trades real money but its LEGO_SYMBOL or "
                "LEGO_FIX_C cannot be read: the cash it may need is unknown")
            continue
        reserve.append(f"{name}:{other}:{_plain(principal)}")
    if reserve:
        add("reserve", INFO, "the account preflight keeps the principal of " + ", ".join(reserve))

    blocking = [f["id"] for f in findings if f["severity"] == BLOCK]
    return {"schema": SCHEMA, "ok": not blocking, "target": target,
            "target_exists": current is not None, "symbol": symbol, "bots": bots,
            "reserve": reserve, "findings": findings, "blocking": blocking}


def service_env(service: dict) -> dict:
    """Environment variables of a Cloud Run v1 service (the shape tools/verify_deployment.py reads)."""
    containers = service["spec"]["template"]["spec"]["containers"]
    if not isinstance(containers, list) or len(containers) != 1:
        raise InventoryError("a LEGO function runs exactly one container")
    return {item["name"]: item.get("value") for item in containers[0].get("env", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)}


def _gcloud(runner, *args) -> str:
    command = ["gcloud", *args]
    try:
        result = runner(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InventoryError(f"{' '.join(command[:4])}: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise InventoryError(f"{' '.join(command[:5])} exited {result.returncode}")
    return result.stdout


def read_functions(project: str, region: str, environment: str, runner=None) -> dict:
    """Every LEGO function of *environment* in the region and its environment variables."""
    runner = runner or subprocess.run
    listed = _gcloud(runner, "run", "services", "list", f"--region={region}",
                     f"--project={project}", "--format=value(metadata.name)")
    functions = {}
    for name in lego_function_names(listed.split(), environment):
        raw = _gcloud(runner, "run", "services", "describe", name, f"--region={region}",
                      f"--project={project}", "--format=json")
        try:
            functions[name] = service_env(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            raise InventoryError(f"the service {name} could not be read: {type(exc).__name__}") from exc
    return functions


def _blocked(id_: str, message: str, target: str) -> dict:
    return {"schema": SCHEMA, "ok": False, "target": target, "target_exists": None, "bots": [],
            "reserve": [], "findings": [{"id": id_, "severity": BLOCK, "message": message}],
            "blocking": [id_]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--target", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--retire", default="")
    args = parser.parse_args(argv)
    symbol, retire = args.symbol.strip().upper(), args.retire.strip().upper()
    if not lego_function_names([args.target], args.env):
        report = _blocked("arguments", f"--target {args.target} is not a LEGO function of {args.env}",
                          args.target)
    elif not re.fullmatch(SYMBOL_PATTERN, symbol) or (retire and not re.fullmatch(SYMBOL_PATTERN, retire)):
        report = _blocked("arguments", "--symbol and --retire must be tickers such as USAR", args.target)
    else:
        try:
            functions = read_functions(args.project, args.region, args.env)
        except InventoryError as exc:
            report = _blocked("inventory_unreadable", f"the LEGO functions of {args.project} could not "
                              f"be read ({exc}); nothing can be judged, so nothing is deployed",
                              args.target)
        else:
            report = judge(functions, target=args.target, symbol=symbol,
                           database_url=args.database_url, retire=retire or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for finding in report["findings"]:
        if finding["severity"] != INFO or finding["id"] == "bots":
            print(f"  [{finding['severity']}] {finding['id']}: {finding['message']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
