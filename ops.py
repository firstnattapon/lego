"""Small, explicit operator CLI. Commands default to read-only/dry-run."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import load_runtime_config, release_binding_for


def candidate_hash(root: Path = Path(__file__).parent) -> str:
    if root.resolve() != Path(__file__).parent.resolve():
        raise ValueError("candidate hash รองรับ release root ปัจจุบันเท่านั้น")
    from tools.candidate_manifest import build_manifest
    return build_manifest()["candidate_hash"]


def _candidate() -> tuple[str, str]:
    from tools.candidate_manifest import build_manifest
    manifest = build_manifest()
    return manifest["candidate_hash"], manifest["scope"]


def check_command(_args) -> dict:
    runtime = load_runtime_config()
    local_candidate = candidate_hash()
    import release_horizon
    from execution_limits import ExecutionLimits, ExecutionLimitError
    try:
        limits = ExecutionLimits.parse(runtime.deployment.execution_limits)
        limit_status = "EXPIRED" if datetime.now(timezone.utc) >= limits.end else "CONFIGURED"
    except ExecutionLimitError:
        limit_status = "MISSING_OR_INVALID"
    return {
        "ok": True,
        "environment": runtime.deployment.environment,
        "endpoint": runtime.deployment.endpoint,
        "symbol": runtime.operator.symbol,
        "mode": runtime.operator.mode,
        "active": runtime.operator.active,
        "allow_fractional": runtime.deployment.allow_fractional,
        "config_hash": runtime.operator.config_hash,
        "account_fingerprint": runtime.deployment.account_fingerprint,
        "release_authorized": runtime.deployment.release_is_authorized,
        "prod_live_gate_open": runtime.prod_live_gate_open,
        "new_orders_authorized": runtime.allows_new_broker_mutation,
        "candidate_hash_local": local_candidate,
        "candidate_matches": runtime.deployment.candidate_hash == local_candidate,
        "execution_limits_status": limit_status,
        "session_budget_note": "inspect durable account-symbol counter; configured is not remaining capacity",
        "horizon": release_horizon.assess(
            release_horizon.inputs_from_runtime(runtime), now=datetime.now(timezone.utc)),
    }


def binding_command(_args) -> dict:
    return {"release_binding": release_binding_for()}


# runtime variable -> the override name deploy/cloudshell-all-in-one.sh reads
DEPLOY_OVERRIDES = {
    "WEBULL_ENV": "WEBULL_ENV_OVERRIDE", "LEGO_MODE": "LEGO_MODE_OVERRIDE",
    "LEGO_ACTIVE": "LEGO_ACTIVE_OVERRIDE", "LEGO_SYMBOL": "LEGO_SYMBOL_OVERRIDE",
    "LEGO_FIX_C": "LEGO_FIX_C_OVERRIDE", "LEGO_DIFF": "LEGO_DIFF_OVERRIDE",
    "LEGO_ALLOW_FRACTIONAL": "LEGO_ALLOW_FRACTIONAL_OVERRIDE",
    "LEGO_DNA_BUNDLE": "LEGO_DNA_BUNDLE_OVERRIDE",
    "LEGO_SESSION_KEY_MODE": "LEGO_SESSION_KEY_MODE_OVERRIDE",
    "LEGO_STALE_ORDER_ACTION": "LEGO_STALE_ORDER_ACTION_OVERRIDE",
    "LEGO_STALE_ORDER_SECONDS": "LEGO_STALE_ORDER_SECONDS_OVERRIDE",
    "LEGO_CANCEL_CONFIRM_GRACE_SECONDS": "LEGO_CANCEL_CONFIRM_GRACE_SECONDS_OVERRIDE",
    "LEGO_MAX_ORDER_QUANTITY": "LEGO_MAX_ORDER_QUANTITY_OVERRIDE",
    "LEGO_MAX_ORDER_NOTIONAL_USD": "LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE",
    "LEGO_MAX_SESSION_ORDERS": "LEGO_MAX_SESSION_ORDERS_OVERRIDE",
    "LEGO_TRADING_WINDOW_END": "LEGO_TRADING_WINDOW_END_OVERRIDE",
    "LEGO_RELEASE_AUTHORIZATION": "LEGO_RELEASE_AUTHORIZATION_OVERRIDE",
}
_LIMIT_KEYS = ("LEGO_MAX_ORDER_QUANTITY", "LEGO_MAX_ORDER_NOTIONAL_USD", "LEGO_MAX_SESSION_ORDERS")


def _read_env_file(path) -> dict:
    """KEY=VALUE lines; a blank value means "not set" (the plan supplies it)."""
    values = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        if value:
            values[key] = value
    return values


def release_plan_command(args, *, now=None) -> dict:
    """Plan the next release. Reads configuration only: nothing is deployed,
    written or sent to a broker, and the account id is never printed.

    Computes the approval window, the candidate hash of this checkout, the
    principal-proportional caps, the release binding and (production) the
    acknowledgement, and judges them with release_horizon.assess. Renewal stays
    a reviewed operator action: this makes an unfit release visible before it is
    deployed, which is what the 2026-10-01 expiry lacked.
    """
    import release_horizon as horizon
    from decimal import Decimal
    now = now or datetime.now(timezone.utc)
    env = dict(_read_env_file(args.env_file)) if args.env_file else {}
    env.update({key: value for key, value in os.environ.items()
                if key.startswith(("LEGO_", "WEBULL_")) or key == "FIREBASE_DB_URL"})
    if args.account_id_stdin:           # the deploy script pipes the secret in; never echoed
        env["WEBULL_ACCOUNT_ID"] = sys.stdin.read().strip()
    if args.window_sessions is not None:
        env["LEGO_TRADING_WINDOW_END"] = horizon.format_window_end(
            horizon.window_end_after_sessions(now, args.window_sessions))
    elif args.window_end:
        env["LEGO_TRADING_WINDOW_END"] = args.window_end
    if args.candidate_hash:
        if not re.fullmatch(r"[0-9a-f]{64}", args.candidate_hash):
            raise ValueError("--candidate-hash must be 64 lowercase hex characters")
        candidate, scope = args.candidate_hash, "supplied"
    else:
        candidate, scope = _candidate()
    env["LEGO_CANDIDATE_HASH"] = candidate
    for computed in ("LEGO_RELEASE_AUTHORIZATION", "LEGO_PROD_LIVE_ACK"):
        env.pop(computed, None)                                  # derived below, never reused

    price = Decimal(str(args.reference_price)) if args.reference_price else None
    base = load_runtime_config(env)
    profile = "PROD" if base.deployment.environment == "PROD" else "UAT"
    recommended = horizon.recommend_limits(
        base.operator.principal_usd, price, profile=profile,
        interval_seconds=base.operator.dna_bundle.interval_seconds,
        initial_funding=args.initial_funding)
    if args.recommended_limits:
        if price is None:
            raise ValueError("--recommended-limits needs --reference-price for the quantity cap")
        env.update({key: recommended[key] for key in _LIMIT_KEYS})

    runtime = load_runtime_config(env)
    binding = runtime.deployment.expected_release_binding
    inputs = horizon.inputs_from_runtime(
        runtime, initial_funding=args.initial_funding, reference_price=price)
    assessment = horizon.assess(inputs, now=now)
    limits = runtime.deployment.execution_limits
    funding = "initial-funding" if args.initial_funding else "prefunded"
    acks = horizon.prod_live_acks(inputs, binding) if (
        runtime.deployment.environment == "PROD" and runtime.operator.allows_new_intents) else {}

    deploy_env = {"EXPECTED_CANDIDATE_HASH": candidate}
    deploy_env.update({override: env[name] for name, override in DEPLOY_OVERRIDES.items()
                       if env.get(name)})
    deploy_env["LEGO_RELEASE_AUTHORIZATION_OVERRIDE"] = binding
    if acks.get(funding):
        deploy_env["LEGO_PROD_LIVE_ACK"] = acks[funding]
    if args.initial_funding:
        # The deploy script judges the release again; it must be told this is the funding one.
        deploy_env["LEGO_FUNDING_MODE_OVERRIDE"] = "initial-funding"
    script = ("deploy/continuous-uat.sh" if runtime.deployment.environment == "UAT"
              else "deploy/cloudshell-all-in-one.sh")

    steps = [
        "Use a clean checkout of the reviewed commit: the candidate hash is computed from "
        "its files and deploy refuses a dirty tree.",
        "python ops.py status   # nothing in flight, no operator halt, no unresolved fence",
        f"Export the deploy_env block below and run: bash {script}",
        "Apply tools/monitoring_config.py output and set ALERT_WEBHOOK_SECRET_OVERRIDE so the "
        "renewal alerts reach a person.",
        f"Nothing renews itself: run this command again before {limits[3]} "
        "(the horizon alert fires 48 hours ahead).",
    ]
    if runtime.deployment.environment == "PROD":
        steps.insert(2, "PROD also needs WEBULL_TOKEN_SECRET_OVERRIDE (operator-issued, NORMAL, "
                        "more than 24h left) and a deploy outside the regular session.")
    if args.initial_funding:
        steps.append("This is the FUNDING release (caps are loose on purpose): deploy it with "
                     "LEGO_FUNDING_MODE_OVERRIDE=initial-funding (in deploy_env). After the funding "
                     "fill is confirmed and holdings match, plan and deploy the steady release "
                     "without --initial-funding.")
    result = {
        "read_only": True, "now_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": runtime.deployment.environment, "mode": runtime.operator.mode,
        "active": runtime.operator.active, "symbol": runtime.operator.symbol,
        "principal_usd": runtime.operator.principal_usd, "diff_usd": runtime.operator.diff_usd,
        "allow_fractional": runtime.deployment.allow_fractional,
        "candidate_hash": candidate, "candidate_scope": scope,
        "account_fingerprint": runtime.deployment.account_fingerprint,
        "release": {
            "LEGO_TRADING_WINDOW_END": limits[3], "LEGO_MAX_ORDER_QUANTITY": limits[0],
            "LEGO_MAX_ORDER_NOTIONAL_USD": limits[1], "LEGO_MAX_SESSION_ORDERS": limits[2],
            "LEGO_CANDIDATE_HASH": candidate, "LEGO_RELEASE_AUTHORIZATION": binding},
        "recommended_limits": recommended,
        "assessment": assessment,
        "deploy_env": deploy_env, "next_steps": steps,
    }
    if acks:
        result["prod_live_acks"] = acks
    if args.enforce:
        result["blocked"] = not assessment["ok"]
    return result


def inspect_open_orders_command(_args) -> dict:
    """One complete read; never adopts, places, cancels or clears a fence."""
    import main
    from open_order_blocker import describe
    runtime = load_runtime_config()
    main._init_firebase()
    cfg = main.Config(runtime.operator.symbol, runtime.operator.principal_usd,
                      runtime.operator.diff_usd, runtime.operator.dna_bundle.dna_code,
                      "shannon_demon_lego_v2")
    trade, _ = main.build_clients()
    witness = describe(main.fetch_open_orders(trade, cfg.symbol))
    chain = main.chain_key(cfg)
    for order in witness["orders"]:
        intent = main.read_intent(chain, order["client_order_id"])
        order["current_chain_intent_status"] = (intent or {}).get("status")
        order["ownership"] = "VERIFY_ORIGINAL_INTENT" if intent else "NOT_FOUND_IN_CURRENT_CHAIN"
    return {"read_only": True, "broker_scan_complete": True,
            "environment": runtime.deployment.environment,
            "account_fingerprint": runtime.deployment.account_fingerprint,
            "symbol": cfg.symbol, "chain_key": chain,
            "status": "OPEN_ORDER_BLOCKED" if witness["count"] else "NO_OPEN_ORDERS",
            "real_money_ready": False, **witness}


def bootstrap_command(args) -> dict:
    token_path = Path(args.token_file).resolve()
    lines = token_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 3 or not lines[0].strip() or lines[2] != "NORMAL":
        raise ValueError("token file ต้องเป็น token/expires/NORMAL จำนวน 3 บรรทัด")
    from datetime import datetime, timezone
    from webull_io import _expires_datetime
    expiry = _expires_datetime(lines[1])
    if expiry is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("token expiry must be readable and in the future")
    if not args.secret.startswith("projects/") or "/secrets/" not in args.secret or "/versions/" in args.secret:
        raise ValueError("secret must be projects/PROJECT/secrets/NAME")
    if not args.apply:
        return {"dry_run": True, "secret": args.secret, "bytes": token_path.stat().st_size}
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    response = client.add_secret_version(
        request={"parent": args.secret, "payload": {"data": token_path.read_bytes()}})
    return {"dry_run": False, "secret_version": response.name}


def status_command(_args) -> dict:
    import main
    main._init_firebase()
    runtime = load_runtime_config()
    cfg = main.Config(
        runtime.operator.symbol, runtime.operator.principal_usd,
        runtime.operator.diff_usd, runtime.operator.dna_bundle.dna_code,
        "shannon_demon_lego_v2")
    state = main.read_chain_state(cfg) or {}
    identity = main.runtime_identity_fingerprint()
    from lego_outbox import DISPATCH_LOCK_PATH, read_intent
    scope = main.account_symbol_fence_key(identity, cfg.symbol)
    fence = main.db.reference(f"{DISPATCH_LOCK_PATH}/{scope}").get() or {}
    active_id = fence.get("inflight_run_id")
    intent = (read_intent(fence.get("inflight_chain_key") or main.chain_key(cfg), active_id)
              if active_id else {}) or {}
    return {
        "environment": runtime.deployment.environment,
        "schema_version": state.get("schema_version"),
        "last_success": state.get("updated_at"),
        "dna_step": state.get("dna_step"),
        "finalized_seq": (state.get("execution_cashflow") or {}).get("finalized_seq"),
        "active_intent_id": active_id,
        "execution_status": intent.get("status"),
        "broker_status": intent.get("broker_status"),
        "cancel_last_error_code": intent.get("cancel_last_error_code"),
        "cancel_refused_at": intent.get("cancel_refused_at"),
        "needs_manual_check": intent.get("needs_manual_check", False),
        "manual_since": intent.get("manual_since"),
        "broker_reject_circuit": fence.get("broker_reject_circuit") or {},
        "operator_halt": fence.get("operator_halt") or {},
        "allow_fractional": runtime.deployment.allow_fractional,
        "broker_fee_status": intent.get("broker_fee_status"),
        "fee_pending_since": intent.get("fee_pending_since"),
        "fee_overdue": intent.get("fee_overdue", False),
        "release_authorized": runtime.deployment.release_is_authorized,
    }


def repair_audit_command(args) -> dict:
    """Repair one historical mirror; never change an intent's execution state."""
    import main
    import execution_service
    from lego_outbox import read_intent, update_intent
    main._init_firebase()
    runtime = load_runtime_config()
    cfg = main.Config(runtime.operator.symbol, runtime.operator.principal_usd,
                      runtime.operator.diff_usd, runtime.operator.dna_bundle.dna_code,
                      "shannon_demon_lego_v2")
    ck = main.chain_key(cfg)
    intent = read_intent(ck, args.run_id)
    if not intent:
        raise ValueError("run_id absent from the configured strategy chain")
    identity = main.runtime_identity_fingerprint()
    if intent.get("runtime_identity_fingerprint") != identity:
        raise ValueError("intent runtime identity differs or is unverified")
    audit = main.db.reference(f"webull_lego_order_audit/{args.run_id}").get() or {}
    result = {"dry_run": not args.apply, "run_id": args.run_id,
              "outbox_status": intent["status"], "audit_status_before": audit.get("status")}
    if args.apply:
        durable = update_intent(ck, args.run_id, {"audit_pending": True})
        execution_service._mirror_order_audit(ck, args.run_id, durable)
        result["audit_pending"] = read_intent(ck, args.run_id).get("audit_pending", True)
        if result["audit_pending"]:
            raise RuntimeError("audit remains pending; no execution state was changed")
    return result


def reset_reject_halt_command(args) -> dict:
    import main
    import broker_circuit
    runtime = load_runtime_config()
    if runtime.operator.allows_new_intents:
        raise ValueError("pause trading (LEGO_ACTIVE=false) before resetting a broker halt")
    main._init_firebase()
    return broker_circuit.reset(main.runtime_identity_fingerprint(), runtime.operator.symbol,
                                expected_halt_id=args.halt_id, reason=args.reason,
                                apply=args.apply)


def halt_orders_command(args) -> dict:
    import main
    import operator_halt
    runtime = load_runtime_config()
    main._init_firebase()
    return operator_halt.set_halt(
        main.runtime_identity_fingerprint(), runtime.operator.symbol,
        operator=args.operator, reason=args.reason, apply=args.apply)


def clear_operator_halt_command(args) -> dict:
    import main
    import operator_halt
    runtime = load_runtime_config()
    if runtime.operator.allows_new_intents:
        raise ValueError("pause trading (LEGO_ACTIVE=false) before clearing operator halt")
    main._init_firebase()
    return operator_halt.clear_halt(
        main.runtime_identity_fingerprint(), runtime.operator.symbol,
        expected_halt_id=args.halt_id, operator=args.operator,
        reason=args.reason, apply=args.apply)


def repair_operator_halt_audit_command(args) -> dict:
    import main
    import operator_halt
    runtime = load_runtime_config()
    main._init_firebase()
    identity = main.runtime_identity_fingerprint()
    pending = bool(operator_halt.status(identity, runtime.operator.symbol).get(
        "audit_pending_event"))
    repaired = (operator_halt.repair_audit(identity, runtime.operator.symbol)
                if args.apply else False)
    return {"dry_run": not args.apply, "audit_pending": pending,
            "repaired": repaired}


def migrate_session_command(args) -> dict:
    import main
    from datetime import datetime, timezone
    from execution_limits import migrate_market_day
    runtime = load_runtime_config()
    if runtime.operator.allows_new_intents:
        raise ValueError("pause new orders before session migration")
    main._init_firebase()
    scope = main.account_symbol_fence_key(main.runtime_identity_fingerprint(), runtime.operator.symbol)
    return migrate_market_day(scope, now=datetime.now(timezone.utc), apply=args.apply)


COMMANDS = {"check": check_command, "release-binding": binding_command,
            "release-plan": release_plan_command,
            "inspect-open-orders": inspect_open_orders_command,
            "migrate-market-day": migrate_session_command,
            "bootstrap-auth": bootstrap_command, "status": status_command,
            "repair-audit": repair_audit_command,
            "reset-reject-halt": reset_reject_halt_command,
            "halt-orders": halt_orders_command,
            "clear-operator-halt": clear_operator_halt_command,
            "repair-operator-halt-audit": repair_operator_halt_audit_command}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("release-binding")
    plan = sub.add_parser(
        "release-plan", help="plan the next release (window, caps, binding, ack); deploys nothing")
    window = plan.add_mutually_exclusive_group()
    window.add_argument("--window-sessions", type=int, metavar="N",
                        help="end the approval window at the close of the Nth complete regular session")
    window.add_argument("--window-end", metavar="ISO_UTC",
                        help="explicit window end, e.g. 2026-10-16T20:00:00Z")
    plan.add_argument("--env-file", help="KEY=VALUE policy (e.g. deploy/uat-continuous.env.example); "
                                         "LEGO_*/WEBULL_* in the process environment override it")
    plan.add_argument("--reference-price", help="recent price, used for the quantity cap")
    plan.add_argument("--recommended-limits", action="store_true",
                      help="use the principal-proportional caps (needs --reference-price)")
    plan.add_argument("--initial-funding", action="store_true",
                      help="plan the t0 funding release (first order of a flat account is ~FIX_C)")
    plan.add_argument("--candidate-hash", help="override the candidate hash of this checkout")
    plan.add_argument("--account-id-stdin", action="store_true",
                      help="read WEBULL_ACCOUNT_ID from stdin (used by the deploy script)")
    plan.add_argument("--enforce", action="store_true",
                      help="exit 1 when the release has a blocking finding")
    sub.add_parser("inspect-open-orders")
    boot = sub.add_parser("bootstrap-auth")
    boot.add_argument("--token-file", required=True)
    boot.add_argument("--secret", required=True,
                      help="projects/PROJECT/secrets/NAME")
    boot.add_argument("--apply", action="store_true")
    sub.add_parser("status")
    migrate = sub.add_parser("migrate-market-day")
    migrate.add_argument("--apply", action="store_true")
    repair = sub.add_parser("repair-audit")
    repair.add_argument("--run-id", required=True)
    repair.add_argument("--apply", action="store_true")
    reset = sub.add_parser("reset-reject-halt")
    reset.add_argument("--halt-id", required=True)
    reset.add_argument("--reason", required=True)
    reset.add_argument("--apply", action="store_true")
    halt = sub.add_parser("halt-orders")
    halt.add_argument("--operator", required=True)
    halt.add_argument("--reason", required=True)
    halt.add_argument("--apply", action="store_true")
    clear = sub.add_parser("clear-operator-halt")
    clear.add_argument("--halt-id", required=True)
    clear.add_argument("--operator", required=True)
    clear.add_argument("--reason", required=True)
    clear.add_argument("--apply", action="store_true")
    repair_halt = sub.add_parser("repair-operator-halt-audit")
    repair_halt.add_argument("--apply", action="store_true")
    return cli


def main_cli() -> int:
    args = parser().parse_args()
    result = COMMANDS[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if isinstance(result, dict) and result.get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
