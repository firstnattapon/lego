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
    return {
        "ok": True,
        "environment": runtime.deployment.environment,
        "endpoint": runtime.deployment.endpoint,
        "symbol": runtime.operator.symbol,
        "mode": runtime.operator.mode,
        "active": runtime.operator.active,
        "config_hash": runtime.operator.config_hash,
        "account_fingerprint": runtime.deployment.account_fingerprint,
        "release_authorized": runtime.deployment.release_is_authorized,
        "candidate_hash_local": local_candidate,
        "candidate_matches": runtime.deployment.candidate_hash == local_candidate,
    }


def binding_command(_args) -> dict:
    return {"release_binding": release_binding_for()}


def bootstrap_command(args) -> dict:
    token_path = Path(args.token_file).resolve()
    lines = token_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 3 or lines[2] != "NORMAL":
        raise ValueError("token file ต้องเป็น token/expires/NORMAL จำนวน 3 บรรทัด")
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


COMMANDS = {"check": check_command, "release-binding": binding_command,
            "bootstrap-auth": bootstrap_command, "status": status_command,
            "repair-audit": repair_audit_command}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("release-binding")
    boot = sub.add_parser("bootstrap-auth")
    boot.add_argument("--token-file", required=True)
    boot.add_argument("--secret", required=True,
                      help="projects/PROJECT/secrets/NAME")
    boot.add_argument("--apply", action="store_true")
    sub.add_parser("status")
    repair = sub.add_parser("repair-audit")
    repair.add_argument("--run-id", required=True)
    repair.add_argument("--apply", action="store_true")
    return cli


def main_cli() -> int:
    args = parser().parse_args()
    print(json.dumps(COMMANDS[args.command](args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
