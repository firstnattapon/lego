"""Small, explicit operator CLI. Commands default to read-only/dry-run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import load_runtime_config, release_binding_for


def candidate_hash(root: Path = Path(__file__).parent) -> str:
    if root.resolve() != Path(__file__).parent.resolve():
        raise ValueError("candidate hash รองรับ release root ปัจจุบันเท่านั้น")
    from tools.candidate_manifest import build_manifest
    return build_manifest()["candidate_hash"]


def check_command(_args) -> dict:
    runtime = load_runtime_config()
    local_candidate = candidate_hash()
    from execution_limits import ExecutionLimits, ExecutionLimitError
    from datetime import datetime, timezone
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
        "candidate_hash_local": local_candidate,
        "candidate_matches": runtime.deployment.candidate_hash == local_candidate,
        "execution_limits_status": limit_status,
        "session_budget_note": "inspect durable account-symbol counter; configured is not remaining capacity",
    }


def binding_command(_args) -> dict:
    return {"release_binding": release_binding_for()}


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
    print(json.dumps(COMMANDS[args.command](args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
