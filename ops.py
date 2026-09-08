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
        "candidate_hash_local": candidate_hash(),
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
    return {
        "environment": runtime.deployment.environment,
        "schema_version": state.get("schema_version"),
        "last_success": state.get("updated_at"),
        "dna_step": state.get("dna_step"),
        "finalized_seq": (state.get("execution_cashflow") or {}).get("finalized_seq"),
        "active_intent_id": (state.get("dispatch_fence") or {}).get("intent_id"),
        "release_authorized": runtime.deployment.release_is_authorized,
    }


COMMANDS = {"check": check_command, "release-binding": binding_command,
            "bootstrap-auth": bootstrap_command, "status": status_command}


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
    return cli


def main_cli() -> int:
    args = parser().parse_args()
    print(json.dumps(COMMANDS[args.command](args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())

